"""Deterministic inventory of bounded BHM resource contracts.

This registry is intentionally an evidence surface first: it records the
currently enforced cross-cutting limits and names the remaining boundary
families that still require per-call-site closure. It must not be treated as a
claim that every filesystem/process route is already centrally configured.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .local_endpoint_policy import MAX_RESPONSE_BYTES


RESOURCE_LIMITS_SCHEMA_VERSION = "bhm.resource-limits.v1"
PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS = 30
PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS = 60
PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS = 120
PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS = 30
PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS = 15
PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS = 60
PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS = 5
PROCESS_EXECUTION_RELEASE_TRUST_GIT_TIMEOUT_SECONDS = 5
PROCESS_EXECUTION_RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS = 15
PROCESS_EXECUTION_RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS = 10
PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS = 60
PROCESS_EXECUTION_RELEASE_SIGNATURE_TIMEOUT_SECONDS = 30
PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS = 30
PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS = 120
PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS = 120
PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS = 15
PROCESS_EXECUTION_DOCKER_CHECK_TIMEOUT_SECONDS = 3
PROCESS_EXECUTION_DOCKER_RECOVERY_TIMEOUT_SECONDS = 20
PROCESS_EXECUTION_P15_STARTUP_TIMEOUT_SECONDS = 20
PROCESS_EXECUTION_P15_LATENCY_TIMEOUT_SECONDS = 90
PROCESS_EXECUTION_LLM_INVENTORY_HARDWARE_TIMEOUT_SECONDS = 5
PROCESS_EXECUTION_GPU_SNAPSHOT_TIMEOUT_SECONDS = 2
PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS = 5
PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS = 3
PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS = 600
PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS = 2.0
RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS = 10
LOCAL_SOCKET_PROBE_TIMEOUT_SECONDS = 0.25
SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
SQLITE_PARSER_BACKUP_TIMEOUT_SECONDS = 30.0
SQLITE_READINESS_PROBE_TIMEOUT_SECONDS = 1.0
SQLITE_HOOK_QUEUE_BUSY_TIMEOUT_SECONDS = 5.0
LLM_HTTP_TIMEOUT_SECONDS = 120
LLM_REFLECTION_TIMEOUT_SECONDS = 30
LLM_INVENTORY_HTTP_TIMEOUT_SECONDS = 20
LLM_SECURITY_REVIEW_TIMEOUT_SECONDS = 90
BHM_INTERNAL_HTTP_TIMEOUT_SECONDS = 15
BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS = 3
EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS = 20
QDRANT_SDK_TIMEOUT_SECONDS = 10
QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS = 2.0
QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS = 30
SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS = 45
MCP_BROKER_JOIN_TIMEOUT_SECONDS = 3.0
MCP_BROKER_CAPACITY_WAIT_SECONDS = 0.2
MCP_BROKER_WAKE_TIMEOUT_SECONDS = 0.2
MCP_SESSION_ADMISSION_TIMEOUT_SECONDS = 30.0
LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS = 2.0
LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS = 1.0
LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS = 3.0
LAUNCHER_TELEMETRY_TIMEOUT_SECONDS = 15.0
LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS = 45.0
LAUNCHER_SERVICE_READINESS_POLL_SECONDS = 1.0
LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS = 4.0


@dataclass(frozen=True)
class ResourceLimit:
    key: str
    surface: str
    value: int | float
    unit: str
    source: str
    env_var: str | None = None


RESOURCE_LIMITS: tuple[ResourceLimit, ...] = (
    ResourceLimit("process.execution_timeout", "process", PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS"),
    ResourceLimit("process.validator_timeout", "process", PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS"),
    ResourceLimit("process.long_validator_timeout", "process", PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS"),
    ResourceLimit("process.git_probe_timeout", "process", PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS"),
    ResourceLimit("process.operator_control_timeout", "process", PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS"),
    ResourceLimit("process.doctor_timeout", "process", PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS"),
    ResourceLimit("process.shutdown_timeout", "process", PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS"),
    ResourceLimit("process.release_trust_git_timeout", "process", PROCESS_EXECUTION_RELEASE_TRUST_GIT_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_RELEASE_TRUST_GIT_TIMEOUT_SECONDS"),
    ResourceLimit("process.release_materialize_git_timeout", "process", PROCESS_EXECUTION_RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS"),
    ResourceLimit("process.release_source_tree_git_timeout", "process", PROCESS_EXECUTION_RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS"),
    ResourceLimit("process.release_archive_timeout", "process", PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS"),
    ResourceLimit("process.release_signature_timeout", "process", PROCESS_EXECUTION_RELEASE_SIGNATURE_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_RELEASE_SIGNATURE_TIMEOUT_SECONDS"),
    ResourceLimit("process.source_registry_git_timeout", "process", PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS"),
    ResourceLimit("process.source_registry_clone_timeout", "process", PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS"),
    ResourceLimit("process.source_registry_fetch_timeout", "process", PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS"),
    ResourceLimit("process.container_timeout", "process", PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS"),
    ResourceLimit("process.docker_check_timeout", "process", PROCESS_EXECUTION_DOCKER_CHECK_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_DOCKER_CHECK_TIMEOUT_SECONDS"),
    ResourceLimit("process.docker_recovery_timeout", "process", PROCESS_EXECUTION_DOCKER_RECOVERY_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_DOCKER_RECOVERY_TIMEOUT_SECONDS"),
    ResourceLimit("process.p15_startup_timeout", "process", PROCESS_EXECUTION_P15_STARTUP_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_P15_STARTUP_TIMEOUT_SECONDS"),
    ResourceLimit("process.p15_latency_timeout", "process", PROCESS_EXECUTION_P15_LATENCY_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_P15_LATENCY_TIMEOUT_SECONDS"),
    ResourceLimit("process.llm_inventory_hardware_timeout", "process", PROCESS_EXECUTION_LLM_INVENTORY_HARDWARE_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_LLM_INVENTORY_HARDWARE_TIMEOUT_SECONDS"),
    ResourceLimit("process.gpu_snapshot_timeout", "process", PROCESS_EXECUTION_GPU_SNAPSHOT_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_GPU_SNAPSHOT_TIMEOUT_SECONDS"),
    ResourceLimit("process.pid_inspection_timeout", "process", PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS"),
    ResourceLimit("process.termination_grace", "process", PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS"),
    ResourceLimit("process.launcher_install_timeout", "process", PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS"),
    ResourceLimit("process.safe_patch_cleanup_timeout", "process", PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS, "seconds", "resource_limits.PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS"),
    ResourceLimit("runtime.chronicle_append_timeout", "runtime", RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS, "seconds", "resource_limits.RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS"),
    ResourceLimit("network.local_socket_probe_timeout", "network", LOCAL_SOCKET_PROBE_TIMEOUT_SECONDS, "seconds", "resource_limits.LOCAL_SOCKET_PROBE_TIMEOUT_SECONDS"),
    ResourceLimit("sqlite.default_busy_timeout", "sqlite", SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS, "seconds", "resource_limits.SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS"),
    ResourceLimit("sqlite.parser_backup_timeout", "sqlite", SQLITE_PARSER_BACKUP_TIMEOUT_SECONDS, "seconds", "resource_limits.SQLITE_PARSER_BACKUP_TIMEOUT_SECONDS"),
    ResourceLimit("sqlite.readiness_probe_timeout", "sqlite", SQLITE_READINESS_PROBE_TIMEOUT_SECONDS, "seconds", "resource_limits.SQLITE_READINESS_PROBE_TIMEOUT_SECONDS"),
    ResourceLimit("sqlite.hook_queue_busy_timeout", "sqlite", SQLITE_HOOK_QUEUE_BUSY_TIMEOUT_SECONDS, "seconds", "resource_limits.SQLITE_HOOK_QUEUE_BUSY_TIMEOUT_SECONDS"),
    ResourceLimit("llm.http_timeout", "llm", LLM_HTTP_TIMEOUT_SECONDS, "seconds", "resource_limits.LLM_HTTP_TIMEOUT_SECONDS"),
    ResourceLimit("llm.reflection_timeout", "llm", LLM_REFLECTION_TIMEOUT_SECONDS, "seconds", "resource_limits.LLM_REFLECTION_TIMEOUT_SECONDS"),
    ResourceLimit("llm.inventory_http_timeout", "llm", LLM_INVENTORY_HTTP_TIMEOUT_SECONDS, "seconds", "resource_limits.LLM_INVENTORY_HTTP_TIMEOUT_SECONDS"),
    ResourceLimit("llm.security_review_timeout", "llm", LLM_SECURITY_REVIEW_TIMEOUT_SECONDS, "seconds", "resource_limits.LLM_SECURITY_REVIEW_TIMEOUT_SECONDS"),
    ResourceLimit("outbound.bhm_internal_timeout", "outbound-http", BHM_INTERNAL_HTTP_TIMEOUT_SECONDS, "seconds", "resource_limits.BHM_INTERNAL_HTTP_TIMEOUT_SECONDS"),
    ResourceLimit("outbound.bhm_speculative_search_timeout", "outbound-http", BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS, "seconds", "resource_limits.BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS"),
    ResourceLimit("outbound.external_search_timeout", "outbound-http", EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS, "seconds", "resource_limits.EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS"),
    ResourceLimit("qdrant.sdk_timeout", "qdrant-sdk", QDRANT_SDK_TIMEOUT_SECONDS, "seconds", "resource_limits.QDRANT_SDK_TIMEOUT_SECONDS"),
    ResourceLimit("qdrant.health_http_timeout", "qdrant-health-http", QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS, "seconds", "resource_limits.QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS"),
    ResourceLimit("qdrant.operator_http_timeout", "qdrant-operator-http", QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS, "seconds", "resource_limits.QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS"),
    ResourceLimit("source_registry.web_timeout", "source-registry-web", SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS, "seconds", "resource_limits.SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS"),
    ResourceLimit("mcp.broker_join_timeout", "mcp", MCP_BROKER_JOIN_TIMEOUT_SECONDS, "seconds", "resource_limits.MCP_BROKER_JOIN_TIMEOUT_SECONDS"),
    ResourceLimit("mcp.broker_capacity_wait", "mcp", MCP_BROKER_CAPACITY_WAIT_SECONDS, "seconds", "resource_limits.MCP_BROKER_CAPACITY_WAIT_SECONDS"),
    ResourceLimit("mcp.broker_wake_timeout", "mcp", MCP_BROKER_WAKE_TIMEOUT_SECONDS, "seconds", "resource_limits.MCP_BROKER_WAKE_TIMEOUT_SECONDS"),
    ResourceLimit("mcp.session_admission_timeout", "mcp", MCP_SESSION_ADMISSION_TIMEOUT_SECONDS, "seconds", "resource_limits.MCP_SESSION_ADMISSION_TIMEOUT_SECONDS"),
    ResourceLimit("launcher.http_probe_timeout", "launcher", LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS, "seconds", "resource_limits.LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS"),
    ResourceLimit("launcher.tcp_probe_timeout", "launcher", LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS, "seconds", "resource_limits.LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS"),
    ResourceLimit("launcher.remote_http_timeout", "launcher", LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS, "seconds", "resource_limits.LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS"),
    ResourceLimit("launcher.telemetry_timeout", "launcher", LAUNCHER_TELEMETRY_TIMEOUT_SECONDS, "seconds", "resource_limits.LAUNCHER_TELEMETRY_TIMEOUT_SECONDS"),
    ResourceLimit("launcher.service_readiness_timeout", "launcher", LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS, "seconds", "resource_limits.LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS"),
    ResourceLimit("launcher.service_readiness_poll", "launcher", LAUNCHER_SERVICE_READINESS_POLL_SECONDS, "seconds", "resource_limits.LAUNCHER_SERVICE_READINESS_POLL_SECONDS"),
    ResourceLimit("launcher.ui_session_mint_timeout", "launcher", LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS, "seconds", "resource_limits.LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS"),
    ResourceLimit("outbound.internal_response_bytes", "outbound-http", MAX_RESPONSE_BYTES, "bytes", "local_endpoint_policy.MAX_RESPONSE_BYTES"),
    ResourceLimit("auth.project_inspection_body", "auth", 1_048_576, "bytes", "caller_auth.MAX_PROJECT_INSPECTION_BYTES"),
    ResourceLimit("ui.exchange_body", "ui", 16_384, "bytes", "app.MAX_UI_EXCHANGE_BODY_BYTES"),
    ResourceLimit("observation.input", "observation", 262_144, "bytes", "observation_security.OBSERVATION_MAX_INPUT_BYTES"),
    ResourceLimit("observation.compact_input", "observation", 524_288, "bytes", "observation_security.OBSERVATION_COMPACT_MAX_INPUT_BYTES"),
    ResourceLimit("observation.idle_input", "observation", 65_536, "bytes", "observation_security.OBSERVATION_IDLE_MAX_INPUT_BYTES"),
    ResourceLimit("observation.sanitized", "observation", 65_536, "bytes", "observation_security.OBSERVATION_MAX_SANITIZED_BYTES"),
    ResourceLimit("observation.string", "observation", 4_096, "chars", "observation_security.OBSERVATION_MAX_STRING_CHARS"),
    ResourceLimit("observation.collection_items", "observation", 128, "items", "observation_security.OBSERVATION_MAX_COLLECTION_ITEMS"),
    ResourceLimit("observation.depth", "observation", 12, "levels", "observation_security.OBSERVATION_MAX_DEPTH"),
    ResourceLimit("context.token_budget", "context", 8_000, "tokens", "context_compiler.MAX_CONTEXT_TOKEN_BUDGET"),
    ResourceLimit("context.item_chars", "context", 1_600, "chars", "context_compiler.MAX_CONTEXT_ITEM_CHARS"),
    ResourceLimit("llm.response_bytes", "llm", 262_144, "bytes", "local_endpoint_policy.MAX_RESPONSE_BYTES"),
    ResourceLimit("llm.response_chars", "llm", 32_000, "chars", "llm_gateway.MAX_RESPONSE_CHARS"),
    ResourceLimit("concurrency.reads", "runtime", 10, "slots", "app._MAX_CONCURRENT_READS", "BHM_MAX_CONCURRENT_READS"),
    ResourceLimit("concurrency.writes", "runtime", 10, "slots", "app._MAX_CONCURRENT_WRITES", "BHM_MAX_CONCURRENT_WRITES"),
    ResourceLimit("queue.reads", "runtime", 20, "items", "app._READ_QUEUE_LIMIT", "BHM_READ_QUEUE_LIMIT"),
    ResourceLimit("queue.writes", "runtime", 20, "items", "app._WRITE_QUEUE_LIMIT", "BHM_WRITE_QUEUE_LIMIT"),
    ResourceLimit("hook.queue_capacity", "hooks", 128, "items", "app._HOOK_QUEUE_CAPACITY", "BHM_HOOK_QUEUE_CAPACITY"),
    ResourceLimit("hook.max_attempts", "hooks", 3, "attempts", "app._HOOK_QUEUE_MAX_ATTEMPTS", "BHM_HOOK_QUEUE_MAX_ATTEMPTS"),
    ResourceLimit("mcp.sessions", "mcp", 32, "sessions", "mcp_streamable_http.MAX_SESSIONS"),
)

OPEN_BOUNDARY_FAMILIES = (
    "process-execution-call-sites",
    "filesystem-call-sites",
    "outbound-http-call-sites",
)


def validate_resource_limits() -> tuple[str, ...]:
    failures: list[str] = []
    keys = [item.key for item in RESOURCE_LIMITS]
    if len(keys) != len(set(keys)):
        failures.append("duplicate resource-limit key")
    for item in RESOURCE_LIMITS:
        if not item.key or not item.surface or not item.source or item.value <= 0:
            failures.append(f"invalid resource-limit entry: {item.key or '<empty>'}")
    return tuple(failures)


def resource_limit_snapshot() -> dict[str, Any]:
    entries = [asdict(item) for item in RESOURCE_LIMITS]
    payload = {
        "schema_version": RESOURCE_LIMITS_SCHEMA_VERSION,
        "status": "baseline",
        "entries": entries,
        "open_boundary_families": list(OPEN_BOUNDARY_FAMILIES),
        "failures": list(validate_resource_limits()),
    }
    digest_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["registry_sha256"] = hashlib.sha256(digest_payload).hexdigest()
    payload["ok"] = not payload["failures"]
    return payload


__all__ = [
    "OPEN_BOUNDARY_FAMILIES",
    "PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_DOCKER_CHECK_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_DOCKER_RECOVERY_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_P15_STARTUP_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_P15_LATENCY_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_LLM_INVENTORY_HARDWARE_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_GPU_SNAPSHOT_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS",
    "PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS",
    "RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS",
    "LOCAL_SOCKET_PROBE_TIMEOUT_SECONDS",
    "SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS",
    "SQLITE_PARSER_BACKUP_TIMEOUT_SECONDS",
    "SQLITE_READINESS_PROBE_TIMEOUT_SECONDS",
    "SQLITE_HOOK_QUEUE_BUSY_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_RELEASE_SIGNATURE_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_RELEASE_TRUST_GIT_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS",
    "PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS",
    "LLM_HTTP_TIMEOUT_SECONDS",
    "LLM_REFLECTION_TIMEOUT_SECONDS",
    "LLM_INVENTORY_HTTP_TIMEOUT_SECONDS",
    "LLM_SECURITY_REVIEW_TIMEOUT_SECONDS",
    "BHM_INTERNAL_HTTP_TIMEOUT_SECONDS",
    "BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS",
    "EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS",
    "QDRANT_SDK_TIMEOUT_SECONDS",
    "QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS",
    "QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS",
    "SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS",
    "MCP_BROKER_JOIN_TIMEOUT_SECONDS",
    "MCP_BROKER_CAPACITY_WAIT_SECONDS",
    "MCP_BROKER_WAKE_TIMEOUT_SECONDS",
    "MCP_SESSION_ADMISSION_TIMEOUT_SECONDS",
    "LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS",
    "LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS",
    "LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS",
    "LAUNCHER_TELEMETRY_TIMEOUT_SECONDS",
    "LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS",
    "LAUNCHER_SERVICE_READINESS_POLL_SECONDS",
    "LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS",
    "RESOURCE_LIMITS",
    "RESOURCE_LIMITS_SCHEMA_VERSION",
    "ResourceLimit",
    "resource_limit_snapshot",
    "validate_resource_limits",
]
