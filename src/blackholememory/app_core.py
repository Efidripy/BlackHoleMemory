"""Domain module app_core: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import time
import asyncio
import copy
import functools
import hashlib
import json
import os
import re
import subprocess
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from qdrant_client.http import models as qdrant_models
from . import app_runtime as rt
from .app_runtime import ResponseTimeout, _env_float, _env_int, _run_bounded_read, _safe_exception_text, _write_json_atomic
from .api_models import SearchRequest, RememberRequest, MemoryUpdateRequest, MemoryUpsertRequest, MemoryCrystallizeRequest, MemoryDetectRequest, BhmHookRequest, BhmHookCompactRequest
from .config import settings
from .resource_limits import PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS
from .resource_limits import BHM_FEDERATED_EMBEDDING_PREPARATION_TIMEOUT_SECONDS
from .resource_limits import BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS
from .capability import configured_admin_capability
from .capability import extract_mcp_capability
from .capability import MCP_OPERATOR_SESSION
from .capability import is_admin_capability_valid
from .caller_auth import authorize_projects
from .ui_origin_policy import request_host_parts
from .embedding_reuse import search_with_precomputed_embedding
from .embedding_cache import EmbeddingCache
from .embedding_cache import embed_query_with_cache
from .mcp_protocol_contract import validate_bhm_remember_arguments
from .ingress_admission import IngressKind
from .ingress_admission import admit_ingress
from .mem0_adapter import StorageNotReady
from .mem0_adapter import decay_lambda_for_payload
from .mem0_adapter import ensure_decay_metadata
from .mem0_adapter import get_global_core_memory
from .mem0_adapter import get_project_mem0_memory
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import global_collection_name
from .mem0_adapter import initial_decay_metadata
from .mem0_adapter import lexical_score
from .mem0_adapter import local_collection_name
from .mem0_adapter import memory_decay_score
from .mem0_adapter import storage_runtime_state
from .mcp_surfaces import filter_tools
from .mcp_surfaces import is_tool_allowed
from .mcp_surfaces import requires_admin_capability
from .mcp_surfaces import resolve_mcp_surface
from .mcp_protocol_contract import initialize_capabilities
from .mcp_protocol_contract import negotiate_protocol_version
from .mcp_protocol_contract import ProtocolContractError
from .mcp_streamable_http import BhmStreamableHttpGateway
from .project_registry import normalize_project_key
from .retrieval_filters import build_candidate_filters
from .retrieval_fusion import weighted_rank_fusion
from .retrieval_diversity import mmr_select
from .exact_identifier_retrieval import ExactIdentifierIndex
from .exact_identifier_retrieval import build_exact_identifier_hits
from .exact_identifier_retrieval import exact_identifier_enabled
from .exact_identifier_retrieval import exact_identifier_tokens
from .semantic_readiness import SemanticReadinessCache
from .semantic_readiness import build_readiness_key
from .semantic_readiness import evaluate_semantic_readiness
from .semantic_readiness import project_warmup_state
from .runtime_storage import resolve_runtime_storage_config
from .projection_generation_binding import ProjectionRouteUnavailable
from .projection_generation_binding import resolve_projection_route
from .memory_service import MemoryServiceNotReady
from .utility_feedback import utility_report
from .utility_feedback_store import load_utility_events
from .typed_memory_contract import TypedMemoryContractUnavailable
from .typed_memory_contract import classify_new_memory
from .typed_memory_contract import typed_memory_capability_available
from . import memory_contracts as _memory_contracts
from .vector_routing import route_vector_targets
from .version_manifest import BROKER_VERSION
from .hook_queue import HookJobQueue
from .observation_contract import ObservationIngressV1
from .observation_store import ObservationIdCollision
from .observation_store import ObservationStore
from .retention import parse_timestamp
from .observation_security import PayloadSanitizer
from .observation_security import redact_secret_text
from .usage_telemetry import UsageTelemetry
from .usage_telemetry import monotonic_elapsed_ms
from .usage_telemetry import normalize_operation
from .retrieval_funnel import RetrievalFunnel
from .code_graph import PARSER_REGISTRY_DIGEST
from .repository_index import probe_repository_state
from .error_taxonomy import classify_jsonrpc_error
from .llm_resource_governor import GovernorConfig
from .llm_resource_governor import LLMResourceGovernor
from .typed_memory_contract import require_typed_memory_contract
from .typed_memory_contract import typed_memory_contract_enabled
from .temporal_contract import TemporalContractUnavailable
from .temporal_contract import TemporalValidationError
from .temporal_contract import normalize_temporal_fields
from .temporal_contract import normalize_temporal_timestamp
from .temporal_contract import require_temporal_contract
from .temporal_contract import temporal_capability_available
from .temporal_contract import temporal_contract_enabled
from .temporal_contract import temporal_intent_requested
from .temporal_contract import temporal_matches


MemoryMetadata = _memory_contracts.MemoryMetadata


MemoryClass = _memory_contracts.MemoryClass


MemoryClassSource = _memory_contracts.MemoryClassSource


MemoryEventRole = _memory_contracts.MemoryEventRole


EVENT_ROLE_SCHEMA_VERSION = _memory_contracts.EVENT_ROLE_SCHEMA_VERSION


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


_INFRA_SPAWNED_PIDS: set[int] = set()


_INFRA_SPAWNED_PIDS_LOCK = threading.RLock()


def _env_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().casefold() in {"1", "true", "yes", "on"}


# Provider SDK calls are synchronous and may outlive the response deadline.
# Never put those calls on asyncio's shared default executor: a few cold or
# wedged embedding/Qdrant requests there can starve health, telemetry and
# recovery probes.  The gate is non-blocking so excess provider work fails
# closed into the existing fallback-grace path instead of queueing forever.
_RETRIEVAL_PROVIDER_WORKERS = _env_int("BHM_RETRIEVAL_PROVIDER_WORKERS", 4, 1)


_RETRIEVAL_PROVIDER_EXECUTOR = ThreadPoolExecutor(
    max_workers=_RETRIEVAL_PROVIDER_WORKERS,
    thread_name_prefix="bhm-retrieval-provider",
)


_RETRIEVAL_PROVIDER_SLOTS = threading.BoundedSemaphore(_RETRIEVAL_PROVIDER_WORKERS)


_HOOK_QUEUE_CAPACITY = _env_int("BHM_HOOK_QUEUE_CAPACITY", 128, 1)


_HOOK_COMPACT_WORKERS = _env_int("BHM_HOOK_COMPACT_WORKERS", 1, 1)


_HOOK_IDLE_WORKERS = _env_int("BHM_HOOK_IDLE_WORKERS", 1, 1)


_HOOK_QUEUE_LEASE_SECONDS = _env_float("BHM_HOOK_QUEUE_LEASE_SECONDS", 120.0, 5.0)


_HOOK_QUEUE_DRAIN_SECONDS = _env_float("BHM_HOOK_QUEUE_DRAIN_SECONDS", 10.0, 0.1)


_HOOK_QUEUE_STORES: dict[str, HookJobQueue] = {}


_HOOK_QUEUE_STORES_LOCK = threading.RLock()


_HOOK_QUEUE_STOP_EVENT: asyncio.Event | None = None


_FALLBACK_MODE_ENV = "BHM_FALLBACK_MODE"


_TELEMETRY_INTERVAL_SECONDS = 2.5


_FALLBACK_GRACE_ACTIVE_UNTIL = 0.0


_CUSTOM_REDACTION_MAX_PATTERNS = 16


_BOOT_REPORT_PATH = settings.runtime_dir / "infra" / "boot_report.json"


class EmbeddingPreparationTimeout(ResponseTimeout):
    """A bounded federated query embedding did not complete in time."""


class RetrievalProviderCapacityExceeded(ResponseTimeout):
    """All bounded provider slots are occupied by still-running SDK calls."""


_EMBEDDING_CACHE_MAX_ENTRIES = _env_int("BHM_EMBEDDING_CACHE_MAX_ENTRIES", 256, 1)


_EMBEDDING_CACHE_TTL_SECONDS = _env_float("BHM_EMBEDDING_CACHE_TTL_SECONDS", 300.0, 0.1)


_QUERY_EMBEDDING_CACHE = EmbeddingCache(
    max_entries=_EMBEDDING_CACHE_MAX_ENTRIES,
    ttl_seconds=_EMBEDDING_CACHE_TTL_SECONDS,
)


_USAGE_TELEMETRY = UsageTelemetry()


_RETRIEVAL_FUNNEL = RetrievalFunnel()


_LLM_GOVERNOR_LOCK = threading.RLock()


_LLM_GOVERNOR: LLMResourceGovernor | None = None


_VECTOR_CONTEXT_LOCAL = "LOCAL"


_VECTOR_CONTEXT_GLOBAL = "GLOBAL"


_GRAPH_EXPANSION_RELATIONS = frozenset({"depends_on", "upgrades"})


_GRAPH_EXPANSION_LINKS_PER_NODE = 100


_GRAPH_FUSION_WEIGHT = 0.04


_MMR_LAMBDA = min(_env_float("BHM_MMR_LAMBDA", 0.94, 0.0), 1.0)


_SEMANTIC_READINESS_GATE_ENABLED = _env_enabled("BHM_SEMANTIC_READINESS_GATE", False)


_SEMANTIC_READINESS_CACHE = SemanticReadinessCache(
    ttl_seconds=_env_float("BHM_SEMANTIC_READINESS_CACHE_TTL_SECONDS", 30.0, 1.0)
)


def _configured_fallback_mode() -> str:
    value = os.getenv(_FALLBACK_MODE_ENV, "explicit").strip().lower()
    return value if value in {"explicit", "disabled"} else "disabled"


def _fallback_grace_active() -> bool:
    return time.monotonic() < _FALLBACK_GRACE_ACTIVE_UNTIL


def _mcp_registry_snapshot_path() -> Path:
    configured = os.getenv("BHM_MCP_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured) / "registry.json"
    return settings.runtime_dir / "mcp" / "registry.json"


def _read_json_snapshot(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None


def _safe_fallback_provider_warmup() -> dict[str, Any]:
    status = rt._get_provider_warmup_status()
    return {
        "enabled": bool(status.get("enabled")),
        "ready": bool(status.get("ready")),
        "attempts": int(status.get("attempts") or 0),
        "updated_at": str(status.get("updated_at") or ""),
        "embedding_warmup_enabled": bool(status.get("embedding_warmup_enabled")),
        "embedding_ready": bool(status.get("embedding_ready")),
        "embedding_attempts": int(status.get("embedding_attempts") or 0),
        "embedding_phase": str(status.get("embedding_phase") or ""),
        "memory_warmup_enabled": bool(status.get("memory_warmup_enabled")),
        "memory_ready": bool(status.get("memory_ready")),
        "memory_projects_count": len(status.get("memory_projects") or []),
        "memory_skipped_projects_count": len(status.get("memory_skipped_projects") or []),
        "memory_phase": str(status.get("memory_phase") or ""),
        "error_present": any(
            bool(status.get(key))
            for key in ("last_error", "embedding_last_error", "memory_last_error")
        ),
    }


def _safe_fallback_storage() -> dict[str, Any]:
    state = storage_runtime_state().as_dict()
    return {
        "configured_mode": state.get("configured_mode"),
        "backend": state.get("backend"),
        "readiness": state.get("readiness"),
        "reason": state.get("reason"),
        "database_exists": bool(state.get("database_exists")),
        "database_schema_ready": bool(state.get("database_schema_ready")),
        "parity_confirmed": bool(state.get("parity_confirmed")),
        "writer_offline_confirmed": bool(state.get("writer_offline_confirmed")),
    }


def _fallback_grace_stage(reason: Exception) -> str:
    """Return a bounded, content-free phase for a degraded read response."""

    if isinstance(reason, EmbeddingPreparationTimeout):
        return "embedding_preparation"
    if isinstance(reason, ResponseTimeout):
        return "retrieval_contour"
    if isinstance(reason, ConnectionError):
        return "provider_transport"
    if isinstance(reason, HTTPException):
        return "provider_http"
    return "unknown"


def _fallback_grace_meta(route: str, reason: Exception) -> dict[str, Any]:
    global _FALLBACK_GRACE_ACTIVE_UNTIL
    _FALLBACK_GRACE_ACTIVE_UNTIL = time.monotonic() + (_TELEMETRY_INTERVAL_SECONDS * 2)
    print("[WARN] MCP transport slow, serving data from Fallback Grace disk snapshot", flush=True)
    return {
        "enabled": True,
        "mode": "degraded",
        "policy": _configured_fallback_mode(),
        "read_only": True,
        "route": route,
        "stage": _fallback_grace_stage(reason),
        "reason": type(reason).__name__,
        "registry": _read_json_snapshot(_mcp_registry_snapshot_path()),
        "provider_warmup": _safe_fallback_provider_warmup(),
        "storage": _safe_fallback_storage(),
    }


def _fallback_memory_records(
    *,
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    freshness_days: int | None = None,
) -> list[dict]:
    return [
        item for item in rt._load_live_memories()
        if _memory_matches_filters(
            item,
            project=project,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            concepts=concepts,
            files=files,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
            freshness_days=freshness_days,
        )
    ]


def _hook_queue_path() -> Path:
    return settings.runtime_dir / "live-memory" / "hook-jobs.sqlite3"


def _hook_queue() -> HookJobQueue:
    path = _hook_queue_path().resolve()
    key = str(path)
    with _HOOK_QUEUE_STORES_LOCK:
        queue = _HOOK_QUEUE_STORES.get(key)
        if queue is None:
            queue = HookJobQueue(path, capacity=_HOOK_QUEUE_CAPACITY)
            _HOOK_QUEUE_STORES[key] = queue
        return queue


def _ensure_hook_request_identity(request: BaseModel) -> BaseModel:
    updates: dict[str, Any] = {}
    if not getattr(request, "eventId", None):
        updates["eventId"] = f"obs_bhm_{uuid.uuid4().hex}"
    if not getattr(request, "correlationId", None):
        updates["correlationId"] = str(getattr(request, "sessionId", "") or updates.get("eventId") or "")
    return request.model_copy(update=updates) if updates else request


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
            except Exception as exc:
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
        except Exception as exc:
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
    except Exception:
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


def _request_host_parts(host_header: str | None) -> tuple[str, str] | None:
    return request_host_parts(host_header, canonical_port=settings.port)


_OBSERVATION_STORE_LOCK = threading.RLock()


_OBSERVATION_SQLITE_STORES: dict[str, ObservationStore] = {}


def _request_surface(request: Request) -> str:
    caller_surface = str(request.headers.get("x-bhm-caller-surface") or "").strip().lower()
    return "mcp" if caller_surface == "mcp" else "rest"


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


def _request_actor(request: Request) -> str:
    principal = getattr(request.state, "bhm_caller_principal", None)
    return str(getattr(principal, "caller_id", "authenticated-caller"))


def _shared_memory_read_enabled() -> bool:
    """Return an opt-in switch; the safe default remains no shared-data route."""

    return str(os.getenv("BHM_SHARED_MEMORY_READ_ENABLED", "") or "").strip().casefold() in {"1", "true", "yes"}


def _utility_feedback_report(
    *,
    project: str,
    as_of: str,
    half_life_days: float,
    min_samples: int,
) -> dict[str, Any]:
    """Build a deterministic, project-scoped report from immutable events."""

    canonical_project = rt._canonical_project(project)
    try:
        report = utility_report(
            load_utility_events(rt._memory_service(), project=canonical_project),
            as_of=as_of,
            half_life_days=half_life_days,
            min_samples=min_samples,
        )
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "utility_feedback_report_invalid", "reason": _safe_exception_text(exc)},
        ) from exc
    return {
        **report,
        "project": canonical_project,
        "lifecycle_action": "none",
        "side_effects": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "projection_mutation": False,
        },
    }


def _governed_consolidation_database_path() -> Path:
    return resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path


def _require_typed_memory_boundary(
    *,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
) -> None:
    """Reject typed intent until the additive contract is operator-enabled."""

    if memory_class is None and event_role is None:
        return
    try:
        require_typed_memory_contract(
            capability_available=typed_memory_capability_available(rt._memory_service().path)
        )
    except (TypedMemoryContractUnavailable, OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "typed_memory_contract_unavailable",
                "feature_flag": "BHM_TYPED_MEMORY_CONTRACT_ENABLED",
                "enabled": typed_memory_contract_enabled(),
                "reason": str(exc),
            },
        ) from exc


def _typed_projection_pushdown_ready() -> bool:
    """Return the explicit operator gate for equality pushdown into Qdrant."""

    raw = str(os.getenv("BHM_TYPED_MEMORY_PROJECTION_READY", "false") or "").strip().casefold()
    if raw not in {"1", "true", "yes", "on"}:
        return False
    if not typed_memory_contract_enabled():
        return False
    try:
        return typed_memory_capability_available(rt._memory_service().repository.path)
    except (OSError, sqlite3.Error):
        return False


def _temporal_capability_available_for_runtime() -> bool:
    if not rt._memory_store_is_authoritative():
        return True
    return temporal_capability_available(rt._memory_service().repository.path)


def _require_temporal_memory_boundary(
    *,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    write_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fields = dict(write_fields or {})
    requested = temporal_intent_requested(
        as_of=as_of,
        valid_from=valid_from,
        valid_to=valid_to,
        include_temporal_unknown=True if include_temporal_unknown else None,
        **fields,
    )
    if not requested:
        return {}
    try:
        require_temporal_contract(
            capability_available=_temporal_capability_available_for_runtime()
        )
        if fields:
            return normalize_temporal_fields({"metadata": fields})
        normalize_temporal_timestamp(as_of, "as_of", allow_none=False) if as_of is not None else None
        normalize_temporal_timestamp(valid_from, "query_valid_from")
        normalize_temporal_timestamp(valid_to, "query_valid_to")
        if valid_from is not None and valid_to is not None and normalize_temporal_timestamp(valid_from, "query_valid_from") >= normalize_temporal_timestamp(valid_to, "query_valid_to"):
            raise TemporalValidationError("query_valid_from must be earlier than query_valid_to")
        return {}
    except (TemporalContractUnavailable, TemporalValidationError, OSError, sqlite3.Error) as exc:
        code = str(exc)
        status = 409 if isinstance(exc, TemporalContractUnavailable) else 422
        raise HTTPException(status_code=status, detail={"code": code}) from exc


def _observe_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "observations.sqlite3"


def _observation_store() -> ObservationStore:
    path = _observe_store_path().resolve()
    key = str(path)
    with _OBSERVATION_STORE_LOCK:
        store = _OBSERVATION_SQLITE_STORES.get(key)
        if store is None:
            store = ObservationStore(path)
            _OBSERVATION_SQLITE_STORES[key] = store
        return store


def _checkpoint_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "checkpoints.json"


def _project_map_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "project-maps.json"


def _session_record_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "session-records.json"


def _task_context_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "task-contexts.json"


def _risk_register_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "risk-registers.json"


def _validation_snapshot_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "validation-snapshots.json"


def _policy_profile_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "policy-profile.json"


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


def _fast_semantic_runtime_slo_status() -> str:
    """Return the semantic-request SLO state without re-entering health routes.

    ``bhm/health/slo`` intentionally performs a full readiness/cutover probe,
    which is appropriate for an operator endpoint but far too expensive to
    call again from an already authenticated code-search request.  The
    readiness gate has already verified the authoritative store and outbox;
    this fallback preserves the same fail-closed projection/provider boundary
    when the gate is disabled.
    """

    try:
        if not rt._memory_store_is_authoritative():
            return "breached"
        outbox = rt._memory_service().outbox_status()
        pending = max(int(outbox.get("pending") or 0), 0) + max(int(outbox.get("processing") or 0), 0)
        failed = max(int(outbox.get("failed") or 0), 0) + max(int(outbox.get("dead_letter") or 0), 0)
        if pending or failed:
            return "breached"
        if not bool(rt._get_provider_warmup_status().get("ready")):
            return "breached"
        return "healthy"
    except (MemoryServiceNotReady, OSError, sqlite3.Error, ValueError, TypeError):
        return "breached"


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


def _project_matches(record_project: Any, requested_project: str | None) -> bool:
    """Match a stored project against a canonical id or one of its aliases."""

    if not requested_project:
        return True
    return str(record_project or "").strip() in rt._project_aliases(requested_project)


def _authorize_resource_project(request: Request, project: Any) -> None:
    """Re-check ownership after loading a project-bearing global resource."""

    principal = getattr(request.state, "bhm_caller_principal", None)
    raw_project = str(project or "").strip()
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    if not raw_project:
        raise HTTPException(
            status_code=403,
            detail={"code": "caller_resource_project_missing"},
        )
    project_error = authorize_projects(
        principal,
        (rt._canonical_project(raw_project),),
        require_explicit=True,
    )
    if project_error:
        raise HTTPException(status_code=403, detail={"code": project_error})


def _effective_search_project(project: str | None) -> str:
    """Resolve one fail-closed project scope for public memory search.

    Missing project input means the configured default project, never an
    unscoped search. ``global`` is a reserved storage contour, not a public
    tenant/project selector; global points are queried only as a secondary
    contour and are still filtered by the requested project.
    """

    raw_project = str(project or "").strip()
    candidate = rt._canonical_project(raw_project or settings.qdrant_collection)
    if normalize_project_key(candidate) == "global":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "global_scope_requires_explicit_internal_capability",
                "message": "global is a reserved BHM contour and is not a public project search scope",
            },
        )
    return candidate


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
        or MemoryClass.UNCLASSIFIED.value
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


def _serialize_checkpoint_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "checkpoint_type": record.get("checkpoint_type"),
        "title": record.get("title"),
        "content": record.get("content"),
        "done": record.get("done") or "",
        "next": record.get("next") or "",
        "checks": record.get("checks") or "",
        "risks": record.get("risks") or "",
        "concepts": record.get("concepts") or [],
        "files": record.get("files") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_task_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "task_id": record.get("task_id"),
        "title": record.get("title") or "",
        "intent": record.get("intent") or "",
        "scope_in": record.get("scope_in") or [],
        "scope_out": record.get("scope_out") or [],
        "repo": record.get("repo") or "",
        "owner": record.get("owner") or "",
        "status": record.get("status") or "open",
        "session_id": record.get("session_id") or "",
        "correlation_id": record.get("correlation_id") or "",
        "files_touched": record.get("files_touched") or [],
        "session_record_id": record.get("session_record_id"),
        "memory_id": record.get("memory_id"),
        "done": record.get("done") or "",
        "next": record.get("next") or "",
        "checks": record.get("checks") or "",
        "risks": record.get("risks") or "",
        "decisions": record.get("decisions") or "",
        "validation": record.get("validation") or "",
        "opened_at": record.get("opened_at"),
        "closed_at": record.get("closed_at"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_task_context_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "current_task": record.get("current_task") or "",
        "status": record.get("status") or "",
        "pending_items": record.get("pending_items") or "",
        "guidance": record.get("guidance") or "",
        "next_step": record.get("next_step") or "",
        "files_touched": record.get("files_touched") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_risk_register_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "summary": record.get("summary") or "",
        "top_risks": record.get("top_risks") or [],
        "mitigations": record.get("mitigations") or [],
        "owner": record.get("owner") or "",
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
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
        or MemoryClass.UNCLASSIFIED.value
    ).strip().lower()


def _effective_event_role(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return str(
        record.get("event_role")
        or metadata.get("event_role")
        or MemoryEventRole.UNCLASSIFIED.value
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


def _freshness_matches(record: Mapping[str, Any], freshness_days: int | None) -> bool:
    """Keep only records updated within the requested UTC freshness window."""

    if freshness_days is None:
        return True
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    updated_at = parse_timestamp(
        record.get("updated_at")
        or metadata.get("updated_at")
    )
    if updated_at is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    return updated_at >= cutoff


def _memory_matches_filters(
    record: dict,
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    include_archived: bool = False,
    include_logs: bool = True,
    include_historical: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    freshness_days: int | None = None,
) -> bool:
    metadata = record.get("metadata") or {}
    record_concepts = set(record.get("tags") or [])
    record_files = set(metadata.get("files") or [])
    accepted_projects = rt._project_aliases(project)

    if not _metadata_matches_taxonomy_filters(
        metadata,
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        include_archived=include_archived,
        include_logs=include_logs,
    ):
        return False
    if not _freshness_matches(record, freshness_days):
        return False
    try:
        if not temporal_matches(
            record,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
        ):
            return False
    except TemporalValidationError:
        return False
    if accepted_projects and record.get("project") not in accepted_projects:
        return False
    if memory_type and record.get("memory_type") != memory_type:
        return False
    requested_memory_class = (
        memory_class.value if isinstance(memory_class, MemoryClass) else str(memory_class or "").strip().lower()
    )
    if requested_memory_class and _effective_memory_class(record) != requested_memory_class:
        return False
    requested_event_role = (
        event_role.value if isinstance(event_role, MemoryEventRole) else str(event_role or "").strip().lower()
    )
    if requested_event_role and _effective_event_role(record) != requested_event_role:
        return False
    if (
        not include_historical
        and requested_event_role != MemoryEventRole.TRACE.value
        and _effective_event_role(record) == MemoryEventRole.TRACE.value
    ):
        return False
    if concepts and not set(concepts).issubset(record_concepts):
        return False
    if files and not set(files).issubset(record_files):
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


def _load_checkpoints() -> list[dict]:
    path = _checkpoint_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _load_project_maps() -> list[dict]:
    path = _project_map_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_project_maps(items: list[dict]) -> Path:
    return _write_json_atomic(_project_map_store_path(), items)


def _load_session_records() -> list[dict]:
    path = _session_record_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _load_task_contexts() -> list[dict]:
    path = _task_context_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_risk_registers() -> list[dict]:
    path = _risk_register_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_validation_snapshots() -> list[dict]:
    path = _validation_snapshot_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_policy_profile() -> dict:
    path = _policy_profile_store_path()
    if not path.exists():
        return {
            "max_content_chars": 8000,
            "max_lines": 120,
            "require_project": True,
            "require_memory_type": False,
            "block_secret_like": True,
            "block_raw_logs": False,
            "updated_at": None,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _load_policy_profile_snapshot() -> dict[str, Any]:
    path = _policy_profile_store_path()
    return {
        "path": str(path),
        "exists": path.exists(),
        "profile": _load_policy_profile(),
    }


def _read_boot_report() -> dict[str, Any]:
    try:
        if not _BOOT_REPORT_PATH.exists():
            return {"status": "missing"}
        data = json.loads(_BOOT_REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "ERROR", "error": "boot_report_unavailable", "timestamp": rt._utc_now_iso()}
    if isinstance(data, dict):
        return data
    return {"status": "ERROR", "error": "boot report is not a JSON object", "timestamp": rt._utc_now_iso()}


def _register_infra_pid(pid: int | str | None) -> None:
    try:
        normalized = int(pid or 0)
    except (TypeError, ValueError):
        return
    if normalized <= 0 or normalized == os.getpid():
        return
    with _INFRA_SPAWNED_PIDS_LOCK:
        _INFRA_SPAWNED_PIDS.add(normalized)


def _load_observations() -> list[dict]:
    with _OBSERVATION_STORE_LOCK:
        return _observation_store().load()


def _append_observations(items: list[dict]) -> Path:
    """Append observations to the authoritative SQLite journal."""

    with _OBSERVATION_STORE_LOCK:
        store = _observation_store()
        store.initialize()
        store.append_many(items)
        return store.path


def _append_observation(item: dict) -> None:
    with _OBSERVATION_STORE_LOCK:
        try:
            _observation_store().append(item)
        except ObservationIdCollision as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "observation_event_id_collision",
                    "eventId": exc.event_id,
                },
            ) from exc


def _query_tokens(query: str) -> list[str]:
    return [token for token in re.findall(r"[a-zA-Z0-9_-]+", query.lower()) if token]


def _safe_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _memory_type_weight(item: dict) -> float:
    metadata = item.get("metadata") or {}
    tags = set(metadata.get("tags") or [])
    title = (metadata.get("raw_title") or "").lower()

    weight = 0.0
    if "hybrid-session" in tags or "hybrid session" in title:
        weight += 2.5
    if "conversation" in tags:
        weight += 0.5
    if metadata.get("memory_type") == "architecture":
        weight += 0.5
    if metadata.get("memory_type") == "fact":
        weight += 0.2
    if metadata.get("memory_type") == "bug":
        weight += 0.2
    if metadata.get("project"):
        weight += 0.3
    return weight


def _query_intent_weight(query: str, item: dict) -> float:
    metadata = item.get("metadata") or {}
    title = (metadata.get("raw_title") or "").lower()
    tags = set(metadata.get("tags") or [])
    tokens = set(_query_tokens(query))
    memory_type = metadata.get("memory_type")

    score = 0.0
    checkpoint_like = {"checkpoint", "session", "hybrid", "workflow"}
    if tokens & checkpoint_like:
        if "checkpoint" in title:
            score += 1.5
        if "hybrid-session" in tags:
            score += 1.5
    else:
        if memory_type in {"fact", "bug", "pattern"}:
            score += 1.4
        if "hybrid-session" in tags:
            score -= 0.3
        if "checkpoint" in title:
            score -= 0.6

    if "role" in tokens and "capability" in tokens and "hybrid-session" in tags:
        score += 1.5

    if {"codex", "mcp", "tools"}.issubset(tokens):
        content = (item.get("memory") or "").lower()
        if "tool_search" in content or "plugin" in title or "plugin" in content:
            score += 1.8
        if "figma mcp" in tags or "codex plugin" in tags:
            score += 3.2
        if "mcp preflight" in tags or "oz mcp list" in tags:
            score -= 1.3
        if "connectivity" in tags:
            score -= 0.4

    if {"workspace", "memory", "checkpoint"}.issubset(tokens):
        if "workspace-layers" in tags:
            score += 4.0
        if "project.md" in title or "project.md" in (item.get("memory") or "").lower():
            score += 3.0
        if "hub" in tags:
            score += 1.5
        if "checkpoint" in title and "workspace-layers" not in tags:
            score -= 0.4

    if {"blackholememory", "checkpoint"}.issubset(tokens):
        content = (item.get("memory") or "").lower()
        if "created new blackholememory repo" in content:
            score += 3.0
        if "bootstrap" in content or "bootstrap" in title:
            score += 1.5
        if "initial qdrant compose" in content:
            score += 1.0

    return score


def _mem0_user_ids_for_search(user_id: str) -> list[str]:
    return [user_id]


def _vector_item_matches_search_request(item: dict, request: SearchRequest) -> bool:
    metadata = item.get("metadata") or {}
    metadata_source_id = metadata.get("source_id")
    project = _effective_search_project(request.project)
    live_record = _resolve_live_memory_for_vector_item(item, project)
    if live_record is not None:
        item["metadata"] = _mem0_metadata_for_record(live_record)
        return _memory_matches_filters(
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


def _lexical_signal(query: str, item: dict) -> float:
    metadata = item.get("metadata") or {}
    text = (item.get("memory") or "").lower()
    title = (metadata.get("raw_title") or "").lower()
    tag_blob = " ".join(metadata.get("tags") or []).lower()
    tokens = _query_tokens(query)

    score = 0.0
    if query.lower() in title:
        score += 2.0
    if query.lower() in text:
        score += 1.0

    for token in tokens:
        if token in title:
            score += 0.8
        if token in tag_blob:
            score += 0.8
        if token in text:
            score += 0.25

    return score


def _collapse_duplicates(results: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for item in results:
        key = item.get("hash") or item.get("memory") or item.get("id")
        current = grouped.get(key)
        if current is None:
            grouped[key] = item
            continue

        current_project = (current.get("metadata") or {}).get("project")
        item_project = (item.get("metadata") or {}).get("project")
        current_created = _safe_datetime(current.get("created_at"))
        item_created = _safe_datetime(item.get("created_at"))

        replace = False
        if not current_project and item_project:
            replace = True
        elif current_project == item_project and item_created < current_created:
            replace = True

        if replace:
            grouped[key] = item

    return list(grouped.values())


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
    is_procedural = str(memory_class or "").strip().casefold() == MemoryClass.PROCEDURAL.value
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


def _semantic_graph_node_aliases(item: dict) -> list[str]:
    metadata = item.get("metadata") or {}
    aliases = [
        metadata.get("source_id"),
        item.get("source_id"),
        item.get("id"),
        metadata.get("mem0_hit_id"),
    ]
    return list(dict.fromkeys(str(alias).strip() for alias in aliases if str(alias or "").strip()))


def _mem0_metadata_for_record(record: dict) -> dict[str, Any]:
    metadata = record.get("metadata") or {}
    return {
        **metadata,
        "project": record["project"],
        "source_system": record["source_system"],
        "source_id": record["source_id"],
        "memory_type": record["memory_type"],
        "memory_class": record.get("memory_class") or metadata.get("memory_class") or MemoryClass.UNCLASSIFIED.value,
        "memory_class_source": record.get("memory_class_source") or metadata.get("memory_class_source") or MemoryClassSource.LEGACY_DEFAULT.value,
        "memory_class_confidence": record.get("memory_class_confidence") if record.get("memory_class_confidence") is not None else metadata.get("memory_class_confidence"),
        "event_role": record.get("event_role") or metadata.get("event_role") or MemoryEventRole.UNCLASSIFIED.value,
        "event_role_version": record.get("event_role_version") or metadata.get("event_role_version") or EVENT_ROLE_SCHEMA_VERSION,
        "tags": record["tags"],
        "session_refs": record["session_refs"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _extract_mem0_ids(add_result: Any) -> list[str]:
    if not isinstance(add_result, dict):
        return []
    return [
        str(item["id"])
        for item in add_result.get("results") or []
        if isinstance(item, dict) and item.get("id")
    ]


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


def _write_vector_record(
    *,
    record: dict,
    collection_name: str,
    context_origin: str,
    vector_targets: list[str],
    existing_ids: list[str],
) -> list[str]:
    memory = get_global_core_memory() if context_origin == _VECTOR_CONTEXT_GLOBAL else get_project_mem0_memory(record.get("project"))
    metadata = _vector_metadata_for_record(
        record,
        collection_name=collection_name,
        context_origin=context_origin,
        vector_targets=vector_targets,
    )
    content = record.get("content") or ""
    agent_id = record.get("agent_id") or "workspace"
    updated_ids: list[str] = []
    for mem0_id in existing_ids:
        try:
            memory.update(mem0_id, data=content, metadata=metadata)
        except Exception:
            continue
        updated_ids.append(mem0_id)
    if updated_ids:
        return updated_ids

    add_result = memory.add(
        [{"role": "user", "content": content}],
        user_id=settings.mem0_user_id,
        agent_id=agent_id,
        metadata=metadata,
        infer=False,
    )
    return _extract_mem0_ids(add_result)


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

    local_ids = _write_vector_record(
        record=record,
        collection_name=local_collection,
        context_origin=_VECTOR_CONTEXT_LOCAL,
        vector_targets=vector_targets,
        existing_ids=_vector_id_list(metadata.get("mem0_ids")),
    )
    metadata["mem0_ids"] = local_ids

    global_ids: list[str] = []
    if "global" in vector_targets:
        global_ids = _write_vector_record(
            record=record,
            collection_name=global_collection_name(),
            context_origin=_VECTOR_CONTEXT_GLOBAL,
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
        _require_temporal_memory_boundary(write_fields=temporal_request_fields)
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
    event_role = request.event_role or MemoryEventRole.UNCLASSIFIED
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
        _require_temporal_memory_boundary(write_fields=request_fields)
        existing = normalize_temporal_fields(item)
        merged = {**existing, **request_fields}
        if "observed_at" not in request_fields:
            merged["observed_at"] = now
            merged["observed_at_source"] = "transaction-clock"
        normalized = normalize_temporal_fields({"metadata": merged}, require_observed=True)
        item.update(normalized)


def _update_live_memory(request: MemoryUpdateRequest) -> dict:
    _require_typed_memory_boundary(
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


_HOOK_TRANSIT_SOURCE_LIMIT = 12


def _dedupe_text(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _coerce_hook_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in (chunk.strip() for chunk in re.split(r"[,\n]+", value)) if part]
    if isinstance(value, (list, tuple)):
        return _dedupe_text([str(item) for item in value if item is not None])
    return []


def _hook_data_value(request: BhmHookRequest, *keys: str) -> Any:
    data = request.data or {}
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _hook_transit_items(request: BhmHookCompactRequest) -> list[Any]:
    explicit_values = [
        request.transit_buffer,
        _hook_data_value(request, "transit_buffer", "transitBuffer"),
        _hook_data_value(request, "transit", "buffer", "current_buffer", "currentBuffer"),
        _hook_data_value(request, "messages", "history", "events", "transcript", "context"),
    ]
    items: list[Any] = []
    for value in explicit_values:
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            items.extend(value)
        else:
            items.append(value)
    if not items and request.data:
        items.append(dict(request.data))
    return items[:_HOOK_TRANSIT_SOURCE_LIMIT]


def _extract_hook_source_ids(request: BhmHookCompactRequest, transit_items: list[Any]) -> list[str]:
    source_ids: list[str] = []
    source_ids.extend(_coerce_hook_text_list(request.source_ids))
    for key in ("source_ids", "sourceIds", "memory_ids", "memoryIds"):
        source_ids.extend(_coerce_hook_text_list(_hook_data_value(request, key)))
    for item in transit_items:
        if not isinstance(item, dict):
            continue
        source_ids.extend(
            _coerce_hook_text_list(
                item.get("source_id")
                or item.get("sourceId")
                or item.get("memory_id")
                or item.get("memoryId")
            )
        )
    return _dedupe_text(source_ids)


def _lifecycle_source_ids(request: BhmHookRequest | ObservationIngressV1) -> list[str]:
    """Read only explicit source references for a content-free proposal.

    Lifecycle policy never infers source IDs from transcript text.  A compact
    hook may use its established typed fields; every other ingress must supply
    one of the explicit ID fields in structured data.
    """

    if isinstance(request, BhmHookCompactRequest):
        return _extract_hook_source_ids(request, _hook_transit_items(request))
    data = request.data
    if not isinstance(data, Mapping):
        return []
    source_ids: list[str] = []
    for key in ("source_ids", "sourceIds", "memory_ids", "memoryIds"):
        source_ids.extend(_coerce_hook_text_list(data.get(key)))
    return _dedupe_text(source_ids)


def _list_checkpoints(project: str | None, checkpoint_type: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = _load_checkpoints()
    if project:
        items = [item for item in items if item.get("project") == project]
    if checkpoint_type:
        items = [item for item in items if item.get("checkpoint_type") == checkpoint_type]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _get_latest_checkpoint(project: str, checkpoint_type: str | None = None) -> dict:
    items, total = _list_checkpoints(project=project, checkpoint_type=checkpoint_type, limit=1, offset=0)
    if total < 1 or not items:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    return items[0]


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
        if _memory_matches_filters(item, project=request.project, include_archived=request.include_archived)
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
        text = _normalized_text(item.get("content"))
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


def _list_tasks(project: str | None, status: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    project_name = rt._canonical_project(project) if project else None
    items = [
        item
        for item in rt._load_tasks()
        if (not project_name or item.get("project") == project_name)
        and (not status or item.get("status") == status)
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _get_task_context(project: str) -> dict:
    items = _load_task_contexts()
    for item in items:
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="task context not found")


def _get_risk_register(project: str) -> dict:
    items = _load_risk_registers()
    for item in items:
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="risk register not found")


def _get_validation_snapshot(project: str) -> dict:
    items = _load_validation_snapshots()
    for item in items:
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="validation snapshot not found")


def _query_suggestions(project: str | None = None) -> list[str]:
    items = [
        item for item in rt._load_live_memories()
        if _memory_matches_filters(item, project=project, include_archived=False)
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


def _project_summary_get(project: str) -> dict:
    canonical_project = rt._canonical_project(project)
    record = _find_live_memory_by_upsert_key(canonical_project, f"project-summary:{canonical_project}")
    if record is None:
        raise HTTPException(status_code=404, detail="project summary not found")
    return _serialize_memory_record(record)


def _rerank_vector_results(query: str, results: list[dict]) -> list[dict]:
    reranked: list[tuple[float, dict]] = []
    for item in results:
        total = float(item.get("score") or 0.0)
        total += _lexical_signal(query, item)
        total += _memory_type_weight(item)
        total += _query_intent_weight(query, item)
        reranked.append((total, item))

    reranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in reranked]


def _float_score(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _apply_decay_to_vector_hit(hit: dict, now: datetime) -> dict:
    metadata = ensure_decay_metadata(
        dict(hit.get("metadata") or {}),
        fallback_at=(
            hit.get("last_accessed_at")
            or hit.get("updated_at")
            or hit.get("created_at")
            or (hit.get("metadata") or {}).get("updated_at")
            or (hit.get("metadata") or {}).get("created_at")
            or rt._utc_now_iso()
        ),
    )
    raw_score = _float_score(hit.get("score"))
    final_score = memory_decay_score(metadata, raw_qdrant_score=raw_score, now=now)
    metadata["raw_qdrant_score"] = raw_score
    metadata["decay_score"] = final_score
    metadata["decay_lambda_per_day"] = decay_lambda_for_payload(metadata)
    hit["metadata"] = metadata
    hit["score"] = final_score
    return hit


def _apply_decay_to_vector_hits(hits: list[dict], now: datetime | None = None) -> list[dict]:
    current = now or datetime.now(timezone.utc)
    return [_apply_decay_to_vector_hit(hit, current) for hit in hits]


def _search_hit_content(hit: dict) -> str:
    return str(hit.get("content") or hit.get("memory") or "")


def _search_hit_origin(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    origin = str(hit.get("context_origin") or metadata.get("context_origin") or "").upper()
    return origin if origin in {_VECTOR_CONTEXT_LOCAL, _VECTOR_CONTEXT_GLOBAL} else _VECTOR_CONTEXT_LOCAL


def _search_hit_collection(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    return str(hit.get("vector_collection") or metadata.get("vector_collection") or "")


def _hybrid_hit_key(hit: dict, fallback_index: int) -> str:
    metadata = hit.get("metadata") or {}
    candidates = [
        metadata.get("source_id"),
        hit.get("source_id"),
        metadata.get("upsert_key"),
        hit.get("id"),
        metadata.get("mem0_hit_id"),
    ]
    for candidate in candidates:
        key = str(candidate or "").strip()
        if key:
            return key
    content_key = _normalized_text(_search_hit_content(hit))
    if content_key:
        return f"content::{content_key}"
    return f"hit::{fallback_index}"


def _hybrid_hit_text(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    parts = [
        _search_hit_content(hit),
        metadata.get("raw_title"),
        " ".join(str(tag) for tag in metadata.get("tags") or []),
        " ".join(str(file) for file in metadata.get("files") or []),
        metadata.get("upsert_key"),
    ]
    return " ".join(str(part) for part in parts if str(part or "").strip())


def _rank_hybrid_vector_hits(query: str, hits: list[dict], *, k: int = 60) -> list[dict]:
    if not hits:
        return []

    indexed_hits: list[tuple[int, str, dict]] = []
    seen_keys: set[str] = set()
    for index, hit in enumerate(hits):
        key = _hybrid_hit_key(hit, index)
        if key in seen_keys:
            key = f"{key}::{index}"
        seen_keys.add(key)
        indexed_hits.append((index, key, hit))

    semantic_order = sorted(
        indexed_hits,
        key=lambda entry: (
            float(entry[2].get("score") or 0.0),
            1 if _search_hit_origin(entry[2]) == _VECTOR_CONTEXT_LOCAL else 0,
            str(
                entry[2].get("updated_at")
                or (entry[2].get("metadata") or {}).get("updated_at")
                or (entry[2].get("metadata") or {}).get("created_at")
                or ""
            ),
            -entry[0],
        ),
        reverse=True,
    )
    semantic_ranks = {key: rank for rank, (_, key, _) in enumerate(semantic_order, start=1)}

    lexical_scores = {key: lexical_score(query, _hybrid_hit_text(hit)) for _, key, hit in indexed_hits}
    lexical_order = sorted(
        indexed_hits,
        key=lambda entry: (
            -lexical_scores[entry[1]],
            semantic_ranks[entry[1]],
            entry[0],
        ),
    )
    lexical_ranks = {key: rank for rank, (_, key, _) in enumerate(lexical_order, start=1)}
    graph_candidates = [
        entry
        for entry in indexed_hits
        if float((entry[2].get("metadata") or {}).get("graph_score") or 0.0) > 0.0
    ]
    graph_order = sorted(
        graph_candidates,
        key=lambda entry: (
            -float((entry[2].get("metadata") or {}).get("graph_score") or 0.0),
            entry[0],
        ),
    )
    graph_ranks = {key: rank for rank, (_, key, _) in enumerate(graph_order, start=1)}
    fused_scores = weighted_rank_fusion(
        {
            "semantic": semantic_ranks,
            "lexical": lexical_ranks,
            "graph": graph_ranks,
        },
        k=k,
        # Graph expansions are useful corroboration, not a replacement for a
        # strong semantic/lexical match; keep their influence bounded.
        weights={"graph": _GRAPH_FUSION_WEIGHT},
    )

    ranked = sorted(
        indexed_hits,
        key=lambda entry: (
            -fused_scores[entry[1]],
            semantic_ranks[entry[1]],
            lexical_ranks[entry[1]],
            -float(entry[2].get("score") or 0.0),
            -lexical_scores[entry[1]],
            entry[0],
        ),
    )
    mmr_selections = mmr_select(
        [_hybrid_hit_text(hit) for _, _key, hit in ranked],
        [fused_scores[key] for _, key, _hit in ranked],
        lambda_param=_MMR_LAMBDA,
    )
    diversified: list[dict] = []
    for mmr_rank, selection in enumerate(mmr_selections, start=1):
        _index, key, hit = ranked[selection.index]
        metadata = dict(hit.get("metadata") or {})
        metadata["semantic_rank"] = semantic_ranks[key]
        metadata["lexical_rank"] = lexical_ranks[key]
        if key in graph_ranks:
            metadata["graph_rank"] = graph_ranks[key]
        metadata["fusion_channels"] = [
            "semantic",
            "lexical",
            *(["graph"] if key in graph_ranks else []),
        ]
        metadata["rrf_score"] = fused_scores[key]
        metadata["fusion_score"] = fused_scores[key]
        metadata["mmr_rank"] = mmr_rank
        metadata["mmr_score"] = selection.mmr_score
        metadata["diversity_penalty"] = selection.redundancy
        hit["metadata"] = metadata
        diversified.append(hit)

    return diversified


def _normalize_collection_hit(hit: dict, *, collection_name: str, context_origin: str) -> dict:
    item = dict(hit)
    metadata = dict(item.get("metadata") or {})
    metadata["context_origin"] = context_origin
    metadata["context_origins"] = [context_origin]
    metadata["vector_collection"] = collection_name
    metadata["vector_collections"] = [collection_name]
    content = _search_hit_content(item)
    item["memory"] = content
    item["content"] = content
    item["metadata"] = metadata
    item["context_origin"] = context_origin
    item["vector_collection"] = collection_name
    return item


def _qdrant_payload_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten projection metadata while retaining the canonical flat fields."""

    nested = payload.get("metadata")
    metadata = dict(nested) if isinstance(nested, dict) else {}
    for key, value in payload.items():
        if key not in {"content", "memory", "data", "metadata"}:
            metadata[key] = value
    return metadata


def _qdrant_point_to_vector_hit(
    point: Any,
    *,
    collection_name: str,
    context_origin: str,
    score_override: Any | None = None,
) -> dict | None:
    payload = dict(getattr(point, "payload", None) or {})
    content = str(payload.get("data") or payload.get("memory") or payload.get("content") or "").strip()
    if not content:
        return None
    metadata = _qdrant_payload_metadata(payload)
    metadata["mem0_hit_id"] = str(getattr(point, "id", "") or "")
    raw_score = payload.get("score") or payload.get("decay_score")
    if raw_score in (None, "") and score_override is not None:
        raw_score = score_override
    hit = {
        "id": str(getattr(point, "id", "") or ""),
        "content": content,
        "memory": content,
        "score": _float_score(raw_score),
        "metadata": metadata,
    }
    return _normalize_collection_hit(hit, collection_name=collection_name, context_origin=context_origin)


def _fetch_qdrant_hit_by_source_id_sync(target_id: str, project_name: str) -> dict | None:
    client = get_qdrant_client()
    source_id = str(target_id or "").strip()
    if not source_id:
        return None
    scroll_filter = qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="source_id",
                match=qdrant_models.MatchValue(value=source_id),
            )
        ]
    )
    route = rt._project_projection_route(project_name)
    collections: list[tuple[str, str]] = []
    if route.collection_name is not None:
        collections.append((route.collection_name, _VECTOR_CONTEXT_LOCAL))
    collections.append((global_collection_name(), _VECTOR_CONTEXT_GLOBAL))
    for collection_name, context_origin in collections:
        try:
            points, _offset = client.scroll(
                collection_name=collection_name,
                scroll_filter=scroll_filter,
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            continue
        if not points:
            continue
        return _qdrant_point_to_vector_hit(points[0], collection_name=collection_name, context_origin=context_origin)

    for collection_name, context_origin in collections:
        try:
            points = client.retrieve(
                collection_name=collection_name,
                ids=[source_id],
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            continue
        if not points:
            continue
        return _qdrant_point_to_vector_hit(points[0], collection_name=collection_name, context_origin=context_origin)
    return None


async def _fetch_qdrant_hit_by_source_id(target_id: str, project_name: str) -> dict | None:
    return await asyncio.to_thread(_fetch_qdrant_hit_by_source_id_sync, target_id, project_name)


def _copy_graph_expansion_hit(hit: dict, *, source_id: str, link_type: str, graph_rank: int) -> dict:
    item = dict(hit)
    metadata = dict(item.get("metadata") or {})
    metadata["graph_metadata"] = {
        "is_graph_expansion": True,
        "extended_from": source_id,
        "link_type": link_type,
    }
    metadata["graph_rank"] = graph_rank
    metadata["graph_score"] = round(1.0 / max(graph_rank, 1), 6)
    item["metadata"] = metadata
    return item


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


async def _augment_hits_with_graph_expansion(hits: list[dict], project_name: str, now: datetime) -> list[dict]:
    if not hits:
        return hits

    existing_by_id: dict[str, dict] = {}
    for hit in hits:
        for node_id in _semantic_graph_node_aliases(hit):
            existing_by_id.setdefault(node_id, hit)

    seed_hits = hits[:2]
    seed_ids = {alias for hit in seed_hits for alias in _semantic_graph_node_aliases(hit)}
    expansions_by_source: dict[str, list[dict]] = {}
    promoted_ids: set[str] = set()
    graph_rank = 0

    for seed in seed_hits:
        source_aliases = _semantic_graph_node_aliases(seed)
        source_id = source_aliases[0] if source_aliases else ""
        if not source_aliases or not source_id:
            continue
        links = await asyncio.to_thread(_authoritative_graph_links_sync, source_aliases, project_name)
        for link in links:
            target_id = str(link.get("target_id") or "").strip()
            link_type = str(link.get("edge_type") or "").strip().upper()
            if not target_id or target_id == source_id or target_id in seed_ids or target_id in promoted_ids:
                continue
            target_hit = existing_by_id.get(target_id)
            if target_hit is None:
                target_hit = await _fetch_qdrant_hit_by_source_id(target_id, project_name)
                if target_hit is not None:
                    target_hit = _apply_decay_to_vector_hit(target_hit, now)
            if target_hit is None:
                continue
            graph_rank += 1
            expansions_by_source.setdefault(source_id, []).append(
                _copy_graph_expansion_hit(
                    target_hit,
                    source_id=source_id,
                    link_type=link_type,
                    graph_rank=graph_rank,
                )
            )
            promoted_ids.add(target_id)

    if not promoted_ids:
        return hits

    expanded: list[dict] = []
    for hit in hits:
        aliases = _semantic_graph_node_aliases(hit)
        node_id = aliases[0] if aliases else ""
        if any(alias in promoted_ids for alias in aliases):
            continue
        expanded.append(hit)
        if node_id:
            expanded.extend(expansions_by_source.get(node_id, []))
    return expanded


def _search_memory_collection(
    *,
    query: str,
    project_name: str,
    context_origin: str,
    limit: int,
    candidate_filters: dict[str, Any] | None = None,
    query_embedding: Any | None = None,
) -> list[dict]:
    if not query.strip():
        return []
    if context_origin == _VECTOR_CONTEXT_GLOBAL:
        collection_name = global_collection_name()
        memory = get_global_core_memory()
    else:
        legacy_collection = local_collection_name(project_name)
        database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
        route = resolve_projection_route(
            project=project_name,
            legacy_collection=legacy_collection,
            database=database_path,
            embedding_model=settings.mem0_embedding_model,
            vector_dimensions=settings.mem0_embedding_dims,
            distance="Cosine",
        )
        if route.collection_name is None:
            raise ProjectionRouteUnavailable(
                f"local projection route unavailable: {route.reason}"
            )
        collection_name = route.collection_name
        memory = get_project_mem0_memory(
            project_name,
            collection_name=collection_name,
            ensure_collection=collection_name == legacy_collection,
        )

    filters = candidate_filters or {"user_id": settings.mem0_user_id}
    if query_embedding is None:
        result = memory.search(query, top_k=max(limit, 1), filters=filters)
    else:
        result = search_with_precomputed_embedding(
            memory,
            query,
            query_embedding,
            top_k=max(limit, 1),
            filters=filters,
        )
    if isinstance(result, dict):
        raw_hits = result.get("results") or []
    elif isinstance(result, list):
        raw_hits = result
    else:
        raw_hits = []
    normalized_hits = [
        _normalize_collection_hit(hit, collection_name=collection_name, context_origin=context_origin)
        for hit in raw_hits
        if isinstance(hit, dict)
    ]
    return normalized_hits[: max(limit, 1)]


def _cached_query_embedding(memory: Any, query: str) -> Any:
    model_key = f"{settings.mem0_embedding_model}:{settings.mem0_embedding_dims}"
    vector, _cache_hit = embed_query_with_cache(
        memory.embedding_model,
        query,
        model_key=model_key,
        cache=_QUERY_EMBEDDING_CACHE,
    )
    return vector


def _consume_late_read_worker(task: asyncio.Task[Any]) -> None:
    """Consume a detached read worker so its eventual exception is not lost."""

    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def _run_bounded_provider_call(worker: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Run one synchronous provider call outside asyncio's shared executor.

    Timed-out SDK calls cannot be force-cancelled.  A dedicated, non-growing
    executor plus a non-blocking slot gate prevents those late calls from
    starving health/readiness probes or building an unbounded queue.
    """

    if not _RETRIEVAL_PROVIDER_SLOTS.acquire(blocking=False):
        raise RetrievalProviderCapacityExceeded("retrieval provider capacity is busy")
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        _RETRIEVAL_PROVIDER_EXECUTOR,
        functools.partial(worker, *args, **kwargs),
    )

    def release_slot(_completed: Any) -> None:
        _RETRIEVAL_PROVIDER_SLOTS.release()

    future.add_done_callback(release_slot)
    return await future


async def _prepare_federated_query_embedding(project_name: str, query: str) -> tuple[Any, float]:
    """Prepare one reusable embedding without letting a cold provider block search.

    The provider is synchronous, so a timed-out thread cannot be stopped safely.
    The caller detaches from it after the bounded response deadline and follows
    the established explicit fallback-grace route.  The late read result is
    consumed and is never used to alter storage, ranking, or the response.
    """

    started = time.perf_counter()

    def prepare() -> Any:
        memory = get_project_mem0_memory(project_name)
        return _cached_query_embedding(memory, query)

    worker_task = asyncio.create_task(_run_bounded_provider_call(prepare))
    try:
        embedding = await asyncio.wait_for(
            asyncio.shield(worker_task),
            timeout=BHM_FEDERATED_EMBEDDING_PREPARATION_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        if worker_task.done():
            # A provider-raised timeout is already part of the existing
            # fallback taxonomy; preserve it instead of calling it a deadline.
            raise
        worker_task.add_done_callback(_consume_late_read_worker)
        raise EmbeddingPreparationTimeout("federated embedding preparation timed out") from exc
    except asyncio.CancelledError:
        worker_task.add_done_callback(_consume_late_read_worker)
        raise
    return embedding, (time.perf_counter() - started) * 1000.0


def _merge_unique_strings(*values: Any) -> list[str]:
    merged: list[str] = []
    for value in values:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        else:
            candidates = []
        for candidate in candidates:
            item = str(candidate).strip()
            if item and item not in merged:
                merged.append(item)
    return merged


def merge_and_sort_hits(local_hits: list[dict], global_hits: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}

    def hit_key(hit: dict, fallback_index: int) -> str:
        content_key = _normalized_text(_search_hit_content(hit))
        if content_key:
            return f"content::{content_key}"
        metadata = hit.get("metadata") or {}
        stable_id = hit.get("id") or hit.get("source_id") or metadata.get("source_id") or metadata.get("upsert_key")
        return f"id::{stable_id or fallback_index}"

    def hit_rank(hit: dict) -> tuple[float, int, str]:
        origin_rank = 1 if _search_hit_origin(hit) == _VECTOR_CONTEXT_LOCAL else 0
        metadata = hit.get("metadata") or {}
        updated = str(hit.get("updated_at") or metadata.get("updated_at") or metadata.get("created_at") or "")
        return (float(hit.get("score") or 0.0), origin_rank, updated)

    for index, hit in enumerate([*local_hits, *global_hits]):
        key = hit_key(hit, index)
        origin = _search_hit_origin(hit)
        collection_name = _search_hit_collection(hit)
        current = merged.get(key)
        if current is None:
            metadata = dict(hit.get("metadata") or {})
            metadata["context_origins"] = _merge_unique_strings(metadata.get("context_origins"), origin)
            metadata["vector_collections"] = _merge_unique_strings(metadata.get("vector_collections"), collection_name)
            hit["metadata"] = metadata
            merged[key] = hit
            continue

        current_metadata = dict(current.get("metadata") or {})
        origins = _merge_unique_strings(current_metadata.get("context_origins"), origin)
        collections = _merge_unique_strings(current_metadata.get("vector_collections"), collection_name)
        if hit_rank(hit) > hit_rank(current):
            next_metadata = dict(hit.get("metadata") or {})
            next_metadata["context_origins"] = origins
            next_metadata["vector_collections"] = collections
            hit["metadata"] = next_metadata
            merged[key] = hit
        else:
            current_metadata["context_origins"] = origins
            current_metadata["vector_collections"] = collections
            current["metadata"] = current_metadata

    ordered = list(merged.values())
    ordered.sort(key=hit_rank, reverse=True)
    return ordered


def _vector_hit_matches_filters(
    hit: dict,
    *,
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    freshness_days: int | None = None,
) -> bool:
    metadata = hit.get("metadata") or {}
    accepted_projects = rt._project_aliases(project)
    if accepted_projects and metadata.get("project") not in accepted_projects:
        return False
    if memory_type and metadata.get("memory_type") != memory_type:
        return False
    requested_memory_class = (
        memory_class.value if isinstance(memory_class, MemoryClass) else str(memory_class or "").strip().lower()
    )
    if requested_memory_class and _effective_memory_class(hit) != requested_memory_class:
        return False
    requested_event_role = (
        event_role.value if isinstance(event_role, MemoryEventRole) else str(event_role or "").strip().lower()
    )
    if requested_event_role and _effective_event_role(hit) != requested_event_role:
        return False
    if (
        not include_historical
        and requested_event_role != MemoryEventRole.TRACE.value
        and _effective_event_role(hit) == MemoryEventRole.TRACE.value
    ):
        return False
    if concepts and not set(concepts).issubset(set(metadata.get("tags") or [])):
        return False
    if files and not set(files).issubset(set(metadata.get("files") or [])):
        return False
    if any(value is not None for value in (as_of, valid_from, valid_to)) or include_temporal_unknown:
        try:
            if not temporal_matches(
                metadata,
                as_of=as_of,
                valid_from=valid_from,
                valid_to=valid_to,
                include_temporal_unknown=include_temporal_unknown,
            ):
                return False
        except TemporalValidationError:
            return False
    if not _freshness_matches(hit, freshness_days):
        return False
    return _metadata_matches_taxonomy_filters(
        metadata,
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        include_archived=include_archived,
        include_logs=include_logs,
    )


def _strict_retrieval_hits(
    hits: list[dict],
    *,
    project_name: str,
    memory_type: str | None = None,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    freshness_days: int | None = None,
    limit: int = 10,
) -> list[dict]:
    """Hydrate candidates from authoritative SQLite before exposing them.

    Qdrant/Mem0 payloads are only ranking hints.  A missing or stale point is
    suppressed rather than trusted, which keeps lifecycle, revision, project
    and typed-filter decisions on the SQLite authority boundary.
    """

    source_ids: list[str] = []
    hit_ids: dict[int, str] = {}
    for index, hit in enumerate(hits):
        metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
        source_id = str(
            metadata.get("source_id")
            or hit.get("source_id")
            or hit.get("id")
            or ""
        ).strip()
        if source_id:
            hit_ids[index] = source_id
            if source_id not in source_ids:
                source_ids.append(source_id)
    if not source_ids:
        return []

    authoritative = {
        str(record.get("source_id") or record.get("id") or ""): record
        for record in rt._memory_service().get_records(
            source_ids,
            project=rt._canonical_project(project_name),
        )
    }
    accepted: list[dict] = []
    for index, hit in enumerate(hits):
        source_id = hit_ids.get(index)
        record = authoritative.get(source_id or "")
        if record is None or not _memory_matches_filters(
            record,
            project=project_name,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            concepts=concepts,
            files=files,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
            freshness_days=freshness_days,
        ):
            continue
        enriched = dict(hit)
        metadata = dict(enriched.get("metadata") or {})
        authoritative_metadata = dict(record.get("metadata") or {})
        metadata.update(
            {
                "source_id": source_id,
                "project": record.get("project"),
                "memory_type": record.get("memory_type"),
                "memory_class": _effective_memory_class(record),
                "memory_class_source": record.get("memory_class_source")
                or authoritative_metadata.get("memory_class_source"),
                "memory_class_confidence": record.get("memory_class_confidence")
                if record.get("memory_class_confidence") is not None
                else authoritative_metadata.get("memory_class_confidence"),
                "event_role": _effective_event_role(record),
                "event_role_version": record.get("event_role_version")
                or authoritative_metadata.get("event_role_version")
                or EVENT_ROLE_SCHEMA_VERSION,
                "lifecycle": record.get("lifecycle") or authoritative_metadata.get("lifecycle"),
                "revision_id": record.get("revision_id") or authoritative_metadata.get("revision_id"),
                "content_sha256": record.get("content_sha256")
                or authoritative_metadata.get("content_sha256"),
                "observed_at": record.get("observed_at") or authoritative_metadata.get("observed_at"),
                "observed_at_source": record.get("observed_at_source")
                or authoritative_metadata.get("observed_at_source"),
                "valid_from": record.get("valid_from") or authoritative_metadata.get("valid_from"),
                "valid_to": record.get("valid_to") or authoritative_metadata.get("valid_to"),
                "open_interval": record.get("open_interval")
                if record.get("open_interval") is not None
                else authoritative_metadata.get("open_interval"),
                "supersedes_revision_id": record.get("supersedes_revision_id")
                or authoritative_metadata.get("supersedes_revision_id"),
                "source_episode_id": record.get("source_episode_id")
                or authoritative_metadata.get("source_episode_id"),
                "source_uri": record.get("source_uri") or authoritative_metadata.get("source_uri"),
                "source_digest": record.get("source_digest") or authoritative_metadata.get("source_digest"),
            }
        )
        enriched["metadata"] = metadata
        enriched["source_id"] = source_id
        enriched["content"] = record.get("content")
        enriched["memory"] = record.get("content")
        accepted.append(enriched)
    return accepted[: max(int(limit), 1)]


@dataclass(frozen=True)
class FederatedSearchOutcome:
    """Backward-compatible federated result plus content-free contour timing."""

    hits: list[dict]
    total: int
    contour_trace: dict[str, Any]

    def __iter__(self):
        """Preserve the established ``hits, total = await federated_search`` API."""

        yield self.hits
        yield self.total


@dataclass(frozen=True)
class _TimedRetrievalContour:
    name: str
    duration_ms: float
    result: Any = None
    error: Exception | None = None
    timed_out: bool = False


async def _run_timed_retrieval_contour(name: str, worker: Any, /, *args: Any, **kwargs: Any) -> _TimedRetrievalContour:
    """Run one read-only contour with a bounded response deadline.

    The Mem0/Qdrant client is synchronous and an in-flight SDK call cannot be
    forcefully cancelled from this async boundary.  The deadline therefore
    protects the caller and lets the sibling contour/fallback proceed; a late
    worker result is deliberately consumed without becoming a response.
    """

    started = time.perf_counter()
    worker_task = asyncio.create_task(_run_bounded_provider_call(worker, *args, **kwargs))
    try:
        result = await asyncio.wait_for(
            asyncio.shield(worker_task),
            timeout=BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS,
        )
        return _TimedRetrievalContour(
            name=name,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            result=result,
        )
    except TimeoutError as exc:
        def consume_late_result(task: asyncio.Task[Any]) -> None:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass

        worker_task.add_done_callback(consume_late_result)
        return _TimedRetrievalContour(
            name=name,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error=exc,
            timed_out=True,
        )
    except Exception as exc:
        return _TimedRetrievalContour(
            name=name,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error=exc,
        )


def _bounded_trace_duration_ms(value: float) -> float:
    return round(max(0.0, min(float(value), 60_000.0)), 3)


def _federated_contour_trace(
    *,
    started: float,
    embedding_duration_ms: float,
    embedding_enabled: bool,
    contours: Sequence[_TimedRetrievalContour],
    exact_enabled: bool,
) -> dict[str, Any]:
    """Return only non-sensitive timings for one completed read-only search."""

    recorded = [
        {
            "name": contour.name,
            "enabled": True,
            "status": "timed_out" if contour.timed_out else "failed" if contour.error is not None else "completed",
            "duration_ms": _bounded_trace_duration_ms(contour.duration_ms),
            "deadline_ms": BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS * 1000.0,
        }
        for contour in contours
    ]
    if not exact_enabled:
        recorded.append({"name": "exact_identifier", "enabled": False, "status": "disabled", "duration_ms": 0.0})
    return {
        "schema_version": "bhm.retrieval-contour-trace.v1",
        "total_duration_ms": _bounded_trace_duration_ms((time.perf_counter() - started) * 1000.0),
        "embedding": {
            "enabled": embedding_enabled,
            "status": "completed" if embedding_enabled else "skipped",
            "duration_ms": _bounded_trace_duration_ms(embedding_duration_ms),
            "deadline_ms": BHM_FEDERATED_EMBEDDING_PREPARATION_TIMEOUT_SECONDS * 1000.0,
        },
        "contours": recorded,
        "storage_mutation": False,
        "ranking_changed": False,
    }


def _build_exact_identifier_hits(
    *,
    query: str,
    project_name: str,
    candidate_count: int,
    memory_type: str | None,
    memory_class: MemoryClass | str | None,
    event_role: MemoryEventRole | str | None,
    domain: str | None,
    semantic_type: str | None,
    priority: str | None,
    as_of: str | None,
    valid_from: str | None,
    valid_to: str | None,
    include_temporal_unknown: bool,
    include_archived: bool,
    include_logs: bool,
    include_historical: bool,
) -> tuple[list[dict], str]:
    """Build and hydrate the opt-in exact lane from bounded SQLite candidates.

    The SQLite route is an operator-migrated project/token access index only.
    Hydration and Python token/filter checks remain authoritative, so a stale
    candidate or cross-project ID cannot become a result.
    This synchronous local worker overlaps its SQLite work with independent
    embedding/vector contours instead of extending their end-to-end tail.
    """

    service = rt._memory_service()
    candidate_ids: list[str] = []
    for token in exact_identifier_tokens(query, query=True):
        for source_id in service.find_exact_identifier_candidate_ids(
            project_name,
            token,
            limit=candidate_count - len(candidate_ids),
        ):
            if source_id not in candidate_ids:
                candidate_ids.append(source_id)
            if len(candidate_ids) >= candidate_count:
                break
        if len(candidate_ids) >= candidate_count:
            break
    authoritative_records = service.get_records(candidate_ids, project=project_name)
    exact_index = ExactIdentifierIndex.build(
        authoritative_records,
        include_record=lambda record: _memory_matches_filters(
            record,
            project=project_name,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
        ),
    )
    exact_source_ids = exact_index.lookup(query, project=project_name, limit=candidate_count)
    hits = build_exact_identifier_hits(
        authoritative_records,
        exact_source_ids,
        project=project_name,
    )
    snapshot_digest = exact_index.snapshot_digest
    for hit in hits:
        hit.setdefault("metadata", {})["exact_identifier_snapshot_digest"] = snapshot_digest
    return hits, snapshot_digest


async def federated_search(
    query: str,
    project_name: str,
    limit: int = 5,
    offset: int = 0,
    *,
    memory_type: str | None = None,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    include_graph_expansion: bool = True,
    include_global: bool = True,
) -> FederatedSearchOutcome:
    started = time.perf_counter()
    _require_typed_memory_boundary(memory_class=memory_class, event_role=event_role)
    _require_temporal_memory_boundary(
        as_of=as_of,
        valid_from=valid_from,
        valid_to=valid_to,
        include_temporal_unknown=include_temporal_unknown,
    )
    project_name = _effective_search_project(project_name)
    include_historical = include_historical or (
        (event_role.value if isinstance(event_role, MemoryEventRole) else str(event_role or "").strip().casefold())
        == MemoryEventRole.TRACE.value
    )
    page_limit = max(min(limit, 200), 1)
    page_offset = max(offset, 0)
    typed_filter_requested = memory_class is not None or event_role is not None
    temporal_filter_requested = (
        as_of is not None
        or valid_from is not None
        or valid_to is not None
        or include_temporal_unknown
    )
    # Typed/temporal retrieval is an authoritative memory query.  Do not fan
    # it out through the code-graph JSON (which is a separate, rebuildable
    # surface and can be very large); graph expansion is reserved for the
    # normal unfiltered context path.
    if typed_filter_requested or temporal_filter_requested or include_historical:
        include_graph_expansion = False
    pushdown_ready = _typed_projection_pushdown_ready() if typed_filter_requested else False
    # Legacy projections may lack typed payload fields.  Keep a bounded wide
    # pool until an operator has proven V2 parity; final filtering remains
    # SQLite-authoritative and therefore cannot silently lose rare classes.
    candidate_count = max(
        page_limit + page_offset,
        20 if (pushdown_ready or not typed_filter_requested) and not temporal_filter_requested else 200,
    )
    candidate_filters = build_candidate_filters(
        user_id=settings.mem0_user_id,
        project_values=rt._project_aliases(project_name),
        memory_type=memory_type,
        memory_class=(
            memory_class.value if isinstance(memory_class, MemoryClass) and pushdown_ready else
            str(memory_class).strip().lower() if memory_class is not None and pushdown_ready else None
        ),
        event_role=(
            event_role.value if isinstance(event_role, MemoryEventRole) and pushdown_ready else
            str(event_role).strip().lower() if event_role is not None and pushdown_ready else None
        ),
        concepts=concepts or [],
        files=files or [],
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        include_archived=include_archived,
        include_logs=include_logs,
        include_historical=include_historical,
    )
    exact_task: asyncio.Task[_TimedRetrievalContour] | None = None
    if query.strip() and exact_identifier_enabled() and exact_identifier_tokens(query, query=True):
        exact_task = asyncio.create_task(
            _run_timed_retrieval_contour(
                "exact_identifier",
                _build_exact_identifier_hits,
                query=query,
                project_name=project_name,
                candidate_count=candidate_count,
                memory_type=memory_type,
                memory_class=memory_class,
                event_role=event_role,
                domain=domain,
                semantic_type=semantic_type,
                priority=priority,
                as_of=as_of,
                valid_from=valid_from,
                valid_to=valid_to,
                include_temporal_unknown=include_temporal_unknown,
                include_archived=include_archived,
                include_logs=include_logs,
                include_historical=include_historical,
            )
    )

    query_embedding = None
    embedding_duration_ms = 0.0
    if query.strip():
        try:
            query_embedding, embedding_duration_ms = await _prepare_federated_query_embedding(project_name, query)
        except Exception:
            if exact_task is not None:
                exact_task.add_done_callback(_consume_late_read_worker)
            raise

    local_task = asyncio.create_task(
        _run_timed_retrieval_contour(
            "local_vector",
            _search_memory_collection,
            query=query,
            project_name=project_name,
            context_origin=_VECTOR_CONTEXT_LOCAL,
            limit=candidate_count,
            candidate_filters=candidate_filters,
            query_embedding=query_embedding,
        )
    )
    tasks: list[asyncio.Task] = [local_task]
    if include_global:
        tasks.append(
            asyncio.create_task(
                _run_timed_retrieval_contour(
                    "global_vector",
                    _search_memory_collection,
                    query=query,
                    project_name=project_name,
                    context_origin=_VECTOR_CONTEXT_GLOBAL,
                    limit=candidate_count,
                    candidate_filters=candidate_filters,
                    query_embedding=query_embedding,
                )
            )
        )

    results = await asyncio.gather(*tasks)
    local_hits: list[dict] = []
    global_hits: list[dict] = []
    errors: list[Exception] = []
    contours = (_VECTOR_CONTEXT_LOCAL, _VECTOR_CONTEXT_GLOBAL) if include_global else (_VECTOR_CONTEXT_LOCAL,)
    for context_origin, contour in zip(contours, results, strict=True):
        if contour.error is not None:
            errors.append(contour.error)
            print(f"[WARN] BHM federated search {context_origin} contour failed: {contour.error}", flush=True)
            continue
        if context_origin == _VECTOR_CONTEXT_LOCAL:
            local_hits = contour.result
        else:
            global_hits = contour.result
    if errors and not local_hits and not global_hits:
        raise errors[0]

    now = datetime.now(timezone.utc)
    local_hits = _apply_decay_to_vector_hits(local_hits, now)
    global_hits = _apply_decay_to_vector_hits(global_hits, now)
    exact_hits: list[dict] = []
    exact_contour: _TimedRetrievalContour | None = None
    if exact_task is not None:
        exact_contour = await exact_task
        if exact_contour.error is not None:
            raise exact_contour.error
        exact_hits, _exact_snapshot_digest = exact_contour.result
        exact_hits = _apply_decay_to_vector_hits(exact_hits, now)
    combined_results = merge_and_sort_hits([*local_hits, *exact_hits], global_hits)
    if temporal_filter_requested:
        # Temporal fields in Qdrant are an optional projection.  Hydrate from
        # SQLite before filtering so stale or missing projection payloads never
        # become authoritative for historical retrieval.
        combined_results = _strict_retrieval_hits(
            combined_results,
            project_name=project_name,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            concepts=concepts,
            files=files,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
            limit=candidate_count,
        )
    filtered = [
        hit
        for hit in combined_results
        if _vector_hit_matches_filters(
            hit,
            project=project_name,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            concepts=concepts,
            files=files,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
        )
    ]
    if include_graph_expansion:
        filtered = await _augment_hits_with_graph_expansion(filtered, project_name, now)
    ranked_hits = _rank_hybrid_vector_hits(query, filtered)
    total = len(ranked_hits)
    trace_contours = [*results]
    if exact_contour is not None:
        trace_contours.append(exact_contour)
    return FederatedSearchOutcome(
        hits=ranked_hits[page_offset : page_offset + page_limit],
        total=total,
        contour_trace=_federated_contour_trace(
            started=started,
            embedding_duration_ms=embedding_duration_ms,
            embedding_enabled=bool(query.strip()),
            contours=trace_contours,
            exact_enabled=exact_task is not None,
        ),
    )


def _llm_governor() -> LLMResourceGovernor:
    global _LLM_GOVERNOR
    with _LLM_GOVERNOR_LOCK:
        if _LLM_GOVERNOR is None:
            _LLM_GOVERNOR = LLMResourceGovernor(GovernorConfig.from_env())
        return _LLM_GOVERNOR


def _code_graph_query_root_id(project: str, root: str | Path) -> str:
    return str(probe_repository_state(Path(root), project=project).root_id)


def _read_git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS,
    ).stdout.strip()
