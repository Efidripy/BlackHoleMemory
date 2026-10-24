"""Bounded read-only MCP Doctor orchestration for canonical Streamable HTTP.

The doctor composes registration, runtime, protocol, catalog and session
contracts into one operator report. It never applies configuration, starts
processes or writes memory state. Legacy stdio/heartbeat ownership is retired
and is not probed or reported as live transport.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .local_endpoint_policy import (
    LocalEndpointError,
    open_local_url,
    read_bounded_response,
    validate_local_endpoint,
)
from .mcp_catalog_contract import CatalogContractError, build_catalog_contract
from .mcp_doctor_base import (
    _ANONYMOUS_BHM_HEALTH_PATHS as _ANONYMOUS_BHM_HEALTH_PATHS,
)
from .mcp_doctor_base import (
    _STREAM_READ_CHUNK_BYTES as _STREAM_READ_CHUNK_BYTES,
)
from .mcp_doctor_base import (
    DEFAULT_BASE_URL as DEFAULT_BASE_URL,
)
from .mcp_doctor_base import (
    MAX_CONNECTION_STATES as MAX_CONNECTION_STATES,
)
from .mcp_doctor_base import (
    MAX_FINGERPRINTS as MAX_FINGERPRINTS,
)
from .mcp_doctor_base import (
    MAX_HTTP_BYTES as MAX_HTTP_BYTES,
)
from .mcp_doctor_base import (
    MAX_ISSUES as MAX_ISSUES,
)
from .mcp_doctor_base import (
    MAX_SCRIPT_BYTES as MAX_SCRIPT_BYTES,
)
from .mcp_doctor_base import (
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from .mcp_doctor_base import (
    VALIDATION_SCHEMA_VERSION as VALIDATION_SCHEMA_VERSION,
)
from .mcp_doctor_base import (
    DoctorConfig as DoctorConfig,
)
from .mcp_doctor_base import (
    _bhm_request_headers as _bhm_request_headers,
)
from .mcp_doctor_base import (
    _bounded_float as _bounded_float,
)
from .mcp_doctor_base import (
    _bounded_int as _bounded_int,
)
from .mcp_doctor_base import (
    _close_process as _close_process,
)
from .mcp_doctor_base import (
    _read_line as _read_line,
)
from .mcp_doctor_base import (
    _read_process_or_user_env_value as _read_process_or_user_env_value,
)
from .mcp_doctor_base import (
    _read_stream_bounded as _read_stream_bounded,
)
from .mcp_doctor_base import (
    _readiness_from_stderr as _readiness_from_stderr,
)
from .mcp_doctor_base import (
    _request as _request,
)
from .mcp_doctor_base import (
    _required_bhm_caller_token as _required_bhm_caller_token,
)
from .mcp_doctor_base import (
    _resolve_runtime_python as _resolve_runtime_python,
)
from .mcp_doctor_base import (
    _run_json_script as _run_json_script,
)
from .mcp_doctor_base import (
    _safe_issue_code as _safe_issue_code,
)
from .mcp_doctor_probes import (
    _configured_sources as _configured_sources,
)
from .mcp_doctor_probes import (
    _connection_snapshot as _connection_snapshot,
)
from .mcp_doctor_probes import (
    _duplicate_fingerprints as _duplicate_fingerprints,
)
from .mcp_doctor_probes import (
    _get_json as _get_json,
)
from .mcp_doctor_probes import (
    _http_mcp_request as _http_mcp_request,
)
from .mcp_doctor_probes import (
    _lease_snapshot as _lease_snapshot,
)
from .mcp_doctor_probes import (
    _ownership_snapshot as _ownership_snapshot,
)
from .mcp_doctor_probes import (
    _protocol_probe as _protocol_probe,
)
from .mcp_doctor_probes import (
    _runtime_snapshot as _runtime_snapshot,
)
from .mcp_doctor_probes import (
    _streamable_http_protocol_probe as _streamable_http_protocol_probe,
)
from .mcp_protocol_contract import CURRENT_PROTOCOL_VERSION
from .mcp_surfaces import CORE_TOOL_NAMES
from .resource_limits import (
    PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS,
)
from .runtime_endpoints import endpoint_url
from .version_manifest import PACKAGE_VERSION

# Preserve module-attribute re-exports for backward compatibility
assert_safe_path = assert_safe_path
CatalogContractError = CatalogContractError
build_catalog_contract = build_catalog_contract
CURRENT_PROTOCOL_VERSION = CURRENT_PROTOCOL_VERSION
endpoint_url = endpoint_url
LocalEndpointError = LocalEndpointError
open_local_url = open_local_url
PACKAGE_VERSION = PACKAGE_VERSION
PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS = PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS
PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS = PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS
read_bounded_response = read_bounded_response
subprocess = subprocess
validate_local_endpoint = validate_local_endpoint


def choose_next_action(
    *,
    runtime: Mapping[str, Any],
    configured: Mapping[str, Any],
    pipe: Mapping[str, Any],
    protocol: Mapping[str, Any],
    leases: Mapping[str, Any],
    ownership: Mapping[str, Any],
    duplicates: Mapping[str, Any],
) -> dict[str, str]:
    """Return one deterministic, bounded operator action."""

    if not runtime.get("reachable"):
        return {
            "severity": "critical",
            "reason_code": "runtime_unreachable",
            "action": "start the authoritative BHM runtime and rerun MCP Doctor",
        }
    if not runtime.get("ready") or not runtime.get("cutover"):
        return {
            "severity": "critical",
            "reason_code": "runtime_not_ready",
            "action": "restore BHM readiness/cutover and rerun MCP Doctor",
        }
    if not runtime.get("slo_ok"):
        if runtime.get("projection_pending") or runtime.get("outbox_pending"):
            action = "drain the authoritative projection outbox, then rerun MCP Doctor"
        else:
            action = "inspect the breached BHM SLO checks, then rerun MCP Doctor"
        return {
            "severity": "high",
            "reason_code": "runtime_slo_breached",
            "action": action,
        }
    if configured.get("status") != "aligned" or configured.get("writes_live_state"):
        return {
            "severity": "high",
            "reason_code": "adapter_drift",
            "action": "run the adapter canary/apply gate and reload only the affected MCP client",
        }
    if not pipe.get("connected"):
        return {
            "severity": "high",
            "reason_code": "streamable_http_unavailable",
            "action": "restore the canonical Streamable HTTP endpoint and rerun MCP Doctor",
        }
    if not protocol.get("ok"):
        return {
            "severity": "high",
            "reason_code": "protocol_handshake_failed",
            "action": "reload the current MCP client and repeat the bounded protocol handshake",
        }
    catalog = (
        protocol.get("catalog") if isinstance(protocol.get("catalog"), Mapping) else {}
    )
    # P26 expands the canonical bounded `bhm` catalog with code-intelligence
    # tools.  The expected count is sourced from the same allowlist that
    # publishes the catalog; hard-coding the historical 12-tool count would
    # falsely report a healthy expanded catalog as broken.
    if not catalog.get("usable") or _bounded_int(
        catalog.get("tool_count"), maximum=128
    ) != len(CORE_TOOL_NAMES):
        return {
            "severity": "high",
            "reason_code": "catalog_unusable",
            "action": "refresh the MCP client catalog and rerun MCP Doctor",
        }
    if leases.get("status") == "unavailable":
        return {
            "severity": "high",
            "reason_code": "streamable_session_status_unavailable",
            "action": "restore the canonical Streamable HTTP status endpoint and rerun MCP Doctor",
        }
    if _bounded_int(leases.get("pending_count"), maximum=64) > 0:
        return {
            "severity": "medium",
            "reason_code": "lease_pending",
            "action": "wait for native lease attachment or reload the MCP client, then rerun MCP Doctor",
        }
    if duplicates.get("status") == "active_conflict":
        return {
            "severity": "high",
            "reason_code": "active_duplicate_registration",
            "action": "remove active MCP alias/drift through the reversible adapter workflow, then rerun MCP Doctor",
        }
    if duplicates.get("status") == "retained_duplicates":
        return {
            "severity": "medium",
            "reason_code": "retained_duplicate_fingerprints",
            "action": "review retained MCP cache aliases separately; do not infer native attach from configuration",
        }
    return {
        "severity": "none",
        "reason_code": "none",
        "action": "no action; keep the current MCP client session and monitor Streamable HTTP session health",
    }


def run_doctor(config: DoctorConfig | None = None) -> dict[str, Any]:
    """Run one bounded, read-only MCP Doctor report."""

    try:
        active_config = config or DoctorConfig()
    except ValueError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "read_only": True,
            "bounded": True,
            "writes_live_state": False,
            "error_code": _safe_issue_code(exc),
            "next_action": {
                "severity": "critical",
                "reason_code": "invalid_doctor_config",
                "action": "fix the bounded MCP Doctor arguments and rerun it",
            },
        }

    started = time.perf_counter()
    configured = _configured_sources(active_config)
    duplicates = _duplicate_fingerprints(active_config)
    runtime = _runtime_snapshot(active_config)
    pipe, protocol = _protocol_probe(active_config)
    leases = _lease_snapshot(active_config)
    connection = _connection_snapshot(active_config)
    ownership = _ownership_snapshot(active_config)
    next_action = choose_next_action(
        runtime=runtime,
        configured=configured,
        pipe=pipe,
        protocol=protocol,
        leases=leases,
        ownership=ownership,
        duplicates=duplicates,
    )
    critical = next_action["severity"] in {"critical", "high"}
    status = (
        "failed"
        if critical
        else "warning"
        if next_action["severity"] == "medium"
        else "healthy"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not critical,
        "status": status,
        "read_only": True,
        "bounded": True,
        "writes_live_state": False,
        "ephemeral_probe_side_effects": [],
        "duration_ms": _bounded_float((time.perf_counter() - started) * 1000.0),
        "configured_sources": configured,
        "duplicates": duplicates,
        "runtime": runtime,
        "pipe": pipe,
        "protocol": protocol,
        "catalog": protocol.get("catalog", {}),
        "leases": leases,
        "connection": connection,
        "process_ownership": ownership,
        "next_action": next_action,
        "privacy": {
            "raw_config_values": False,
            "raw_prompts": False,
            "raw_tool_arguments": False,
            "raw_environment": False,
            "secrets": False,
            "full_process_commands": False,
            "full_identifiers": False,
        },
    }


__all__ = [
    "DEFAULT_BASE_URL",
    "DoctorConfig",
    "SCHEMA_VERSION",
    "VALIDATION_SCHEMA_VERSION",
    "choose_next_action",
    "run_doctor",
]
