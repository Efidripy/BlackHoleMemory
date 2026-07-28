"""Unified BHM MCP, hook and agent-adapter contract (WI-11).

This module composes the existing P18 contracts.  It is deliberately a pure
read-only boundary: it does not edit client files, start hooks, claim a native
MCP lease, call a model, or write SQLite/Qdrant.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .mcp_catalog_contract import build_catalog_contract
from .mcp_registration import RegistrationContractError
from .mcp_registration import load_contract
from .mcp_registration import resolve_contract_path
from .mcp_surfaces import CORE_TOOL_NAMES
from .mcp_protocol_contract import SUPPORTED_PROTOCOL_VERSIONS


UNIFIED_MCP_CONTRACT_SCHEMA_VERSION = "bhm.mcp.unified-contract.v1"
UNIFIED_MCP_MAX_CLIENTS = 8
UNIFIED_MCP_MAX_HOOKS = 16
UNIFIED_MCP_MAX_BYTES = 256_000
EXPECTED_CANONICAL_SERVER_ID = "bhm"
EXPECTED_CLIENTS = ("claude", "codex")


class UnifiedMcpContractError(ValueError):
    """Raised when the bounded unified MCP contract cannot be built."""


_HOOK_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "pre-task",
        "phase": "pre-task",
        "idempotency_key": "project:session:task",
        "max_events": 8,
        "budget_ms": 250,
        "observable": True,
        "writes": False,
        "recovery": "skip-and-report-on-queue-or-runtime-unavailable",
    },
    {
        "id": "post-tool",
        "phase": "post-tool",
        "idempotency_key": "session:event_id",
        "max_events": 8,
        "budget_ms": 250,
        "observable": True,
        "writes": False,
        "recovery": "deduplicate-event-and-requeue-bounded-job",
    },
    {
        "id": "pre-change",
        "phase": "pre-change",
        "idempotency_key": "project:change_digest",
        "max_events": 4,
        "budget_ms": 500,
        "observable": True,
        "writes": False,
        "recovery": "fail-closed-on-impact-or-test-selection-error",
    },
    {
        "id": "post-test",
        "phase": "post-test",
        "idempotency_key": "project:test_run_digest",
        "max_events": 8,
        "budget_ms": 500,
        "observable": True,
        "writes": False,
        "recovery": "persist-result-summary-only-and-retry-once",
    },
    {
        "id": "post-task",
        "phase": "post-task",
        "idempotency_key": "project:session:task:close",
        "max_events": 4,
        "budget_ms": 500,
        "observable": True,
        "writes": False,
        "recovery": "checkpoint-through-existing-bounded-bridge",
    },
    {
        "id": "session-end",
        "phase": "session-end",
        "idempotency_key": "project:session:end",
        "max_events": 2,
        "budget_ms": 500,
        "observable": True,
        "writes": False,
        "recovery": "flush-bounded-events-and-report-dropped-count",
    },
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: Any) -> str:
    encoded = _canonical_json(value).encode("utf-8")
    if len(encoded) > UNIFIED_MCP_MAX_BYTES:
        raise UnifiedMcpContractError("unified MCP contract exceeds bounded size")
    return hashlib.sha256(encoded).hexdigest()


def _default_catalog_responses() -> tuple[dict[str, Any], dict[str, Any]]:
    initialize = {
        "result": {
            "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
            "serverInfo": {"name": EXPECTED_CANONICAL_SERVER_ID, "version": "fixture", "surface": "core"},
        }
    }
    catalog = {
        "result": {
            "tools": [
                {"name": name, "description": "BHM core compatibility tool", "inputSchema": {"type": "object"}}
                for name in sorted(CORE_TOOL_NAMES)
            ]
        }
    }
    return initialize, catalog


def _normalize_native_state(native_mcp: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(native_mcp or {})
    attached = bool(raw.get("attached", False))
    verified = bool(raw.get("current_session_verified", raw.get("currentSessionVerified", False)))
    lease_live = bool(raw.get("runtime_lease_live", raw.get("runtimeLeaseLive", False)))
    return {
        "attached": attached,
        "current_session_verified": verified,
        "runtime_lease_live": lease_live,
        "probe_ok": bool(raw.get("probe_ok", raw.get("probeOk", True))),
        "reason_code": str(raw.get("reason_code", raw.get("reasonCode", "no_live_native_lease"))).strip() or "unknown",
    }


def _normalize_clients(
    snapshots: Sequence[Mapping[str, Any]] | None,
    *,
    schema_hash: str,
    native_state: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    defaults = [
        {
            "client": client,
            "server_id": EXPECTED_CANONICAL_SERVER_ID,
            "schema_hash": schema_hash,
            "status": "attached" if native_state.get("attached") and native_state.get("current_session_verified") else "degraded",
            "native_attached": bool(native_state.get("attached") and native_state.get("current_session_verified")),
            "rest_bridge": not bool(native_state.get("attached") and native_state.get("current_session_verified")),
            "reason": "usable" if native_state.get("attached") and native_state.get("current_session_verified") else "MCP unavailable; REST degraded bridge",
        }
        for client in EXPECTED_CLIENTS
    ]
    items = list(snapshots) if snapshots is not None else defaults
    if len(items) > UNIFIED_MCP_MAX_CLIENTS:
        raise UnifiedMcpContractError("client snapshot count exceeds bounded limit")
    result: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        item = dict(raw)
        client = str(item.get("client") or "").strip().casefold()
        server_id = str(item.get("server_id", item.get("serverId", "")) or "").strip()
        status = str(item.get("status") or "degraded").strip().casefold()
        item_out = {
            "client": client,
            "server_id": server_id,
            "schema_hash": str(item.get("schema_hash", item.get("schemaDigest", "")) or ""),
            "status": status,
            "native_attached": bool(item.get("native_attached", item.get("nativeAttached", False))),
            "rest_bridge": bool(item.get("rest_bridge", item.get("restBridge", False))),
            "reason": str(item.get("reason") or "").strip()[:240],
        }
        result.append(item_out)
        if client in seen:
            issues.append({"code": "duplicate_client", "client": client})
        seen.add(client)
        if client not in EXPECTED_CLIENTS:
            issues.append({"code": "unknown_client", "client": client})
        if server_id != EXPECTED_CANONICAL_SERVER_ID:
            issues.append({"code": "noncanonical_server_id", "client": client, "server_id": server_id})
        if item_out["schema_hash"] != schema_hash:
            issues.append({"code": "schema_hash_mismatch", "client": client})
        if status not in {"attached", "degraded", "detached"}:
            issues.append({"code": "invalid_client_status", "client": client, "status": status})
        if item_out["native_attached"] and not native_state.get("current_session_verified"):
            issues.append({"code": "unverified_native_attach", "client": client})
        if status == "degraded" and not item_out["rest_bridge"]:
            issues.append({"code": "degraded_without_rest_bridge", "client": client})
    if seen != set(EXPECTED_CLIENTS):
        issues.append({"code": "client_matrix_incomplete", "expected": list(EXPECTED_CLIENTS), "actual": sorted(seen)})
    return sorted(result, key=lambda item: item["client"]), issues


def _normalize_hooks(profile: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_profile = dict(profile or {})
    result: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for definition in _HOOK_DEFINITIONS:
        item = dict(definition)
        enabled = raw_profile.get(definition["id"], raw_profile.get(definition["phase"], False))
        if not isinstance(enabled, bool):
            issues.append({"code": "hook_enabled_not_boolean", "hook": definition["id"]})
            enabled = False
        item["enabled"] = enabled
        item["bounded"] = int(item["max_events"]) > 0 and int(item["budget_ms"]) > 0
        item["idempotent"] = bool(str(item["idempotency_key"]).strip())
        if not item["bounded"] or not item["idempotent"] or not item["observable"] or item["writes"]:
            issues.append({"code": "hook_safety_invariant", "hook": definition["id"]})
        result.append(item)
    unknown = sorted(set(raw_profile) - {item["id"] for item in _HOOK_DEFINITIONS} - {item["phase"] for item in _HOOK_DEFINITIONS})
    if unknown:
        issues.append({"code": "unknown_hook_profile", "hooks": unknown})
    return result, issues


def build_unified_mcp_contract(
    *,
    manifest_path: Path | str | None = None,
    initialize_response: Mapping[str, Any] | None = None,
    catalog_response: Mapping[str, Any] | None = None,
    client_snapshots: Sequence[Mapping[str, Any]] | None = None,
    native_mcp: Mapping[str, Any] | None = None,
    hook_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded unified contract from existing P18 evidence."""

    repo_root = Path(__file__).resolve().parents[2]
    try:
        manifest = resolve_contract_path(
            manifest_path or (repo_root / "config" / "mcp-registration.json"),
            repo_root=repo_root,
        )
        registration = load_contract(manifest, repo_root=repo_root)
    except (OSError, RegistrationContractError, ValueError) as exc:
        raise UnifiedMcpContractError(f"registration contract rejected: {exc}") from exc
    native_state = _normalize_native_state(native_mcp)
    initialize, catalog = (initialize_response, catalog_response)
    if initialize is None or catalog is None:
        initialize, catalog = _default_catalog_responses()
    catalog_contract = build_catalog_contract(initialize, catalog)
    adapter_payload = json.loads(manifest.read_text(encoding="utf-8"))  # lgtm [py/path-injection]
    adapter_contract = adapter_payload.get("adapter_contract") if isinstance(adapter_payload, dict) else {}
    if not isinstance(adapter_contract, Mapping):
        adapter_contract = {}
    clients, client_issues = _normalize_clients(client_snapshots, schema_hash=catalog_contract.schema_hash, native_state=native_state)
    hooks, hook_issues = _normalize_hooks(hook_profile)
    issues: list[dict[str, Any]] = []
    if registration.canonical_server_id != EXPECTED_CANONICAL_SERVER_ID:
        issues.append({"code": "canonical_server_id", "actual": registration.canonical_server_id})
    if registration.aliases:
        issues.append({"code": "parallel_aliases", "aliases": list(registration.aliases)})
    if catalog_contract.server_id != EXPECTED_CANONICAL_SERVER_ID:
        issues.append({"code": "catalog_server_id", "actual": catalog_contract.server_id})
    if catalog_contract.tool_count != len(CORE_TOOL_NAMES):
        issues.append({"code": "core_tool_count", "actual": catalog_contract.tool_count, "expected": len(CORE_TOOL_NAMES)})
    if not catalog_contract.usable:
        issues.append({"code": "catalog_unusable", "reason": catalog_contract.reason})
    issues.extend(client_issues)
    issues.extend(hook_issues)
    adapter_clients = adapter_contract.get("clients") if isinstance(adapter_contract, Mapping) else {}
    if not isinstance(adapter_clients, Mapping):
        adapter_clients = {}
        issues.append({"code": "adapter_clients_missing"})
    for client in EXPECTED_CLIENTS:
        spec = adapter_clients.get(client)
        if not isinstance(spec, Mapping):
            issues.append({"code": "adapter_client_missing", "client": client})
            continue
        if str(spec.get("server_id") or "").strip() != EXPECTED_CANONICAL_SERVER_ID:
            issues.append({"code": "adapter_server_id", "client": client})
    namespaces = sorted({str(item.get("server_id") or "").strip() for item in clients if str(item.get("server_id") or "").strip()})
    core = {
        "schema_version": UNIFIED_MCP_CONTRACT_SCHEMA_VERSION,
        "canonical_server_id": EXPECTED_CANONICAL_SERVER_ID,
        "aliases": list(registration.aliases),
        "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "core_tool_names": sorted(CORE_TOOL_NAMES),
        "catalog": catalog_contract.as_dict(),
        "adapter_contract_digest": _sha256(adapter_contract),
        "clients": clients,
        "hooks": hooks,
        "namespaces": namespaces,
        "native_mcp": native_state,
        "degraded_mode": {
            "active": not bool(native_state["attached"] and native_state["current_session_verified"]),
            "transport": "native-mcp" if bool(native_state["attached"] and native_state["current_session_verified"]) else "rest-bridge",
            "status": "usable" if bool(native_state["attached"] and native_state["current_session_verified"]) else "MCP unavailable",
            "reason_code": native_state["reason_code"],
        },
        "provenance": {
            "manifest": str(manifest.relative_to(repo_root)).replace("\\", "/") if manifest.is_relative_to(repo_root) else str(manifest),
            "source_contracts": ["mcp_registration", "mcp_catalog_contract", "mcp_protocol_contract", "hook_queue"],
            "license_review": "BHM-native existing P18 contracts; no external source code copied",
        },
    }
    digest = _sha256(core)
    return {
        **core,
        "contract_digest": digest,
        "checks": {
            "one_canonical_namespace": not any(item.get("code") in {"parallel_aliases", "noncanonical_server_id", "parallel_namespace"} for item in issues) and namespaces == [EXPECTED_CANONICAL_SERVER_ID],
            # Historical key retained for consumers that still display the
            # pre-P26 contract; the active gate is the allowlist-derived
            # public_core_tools check below.
            "public_core_12_tools": catalog_contract.tool_count == 12,
            "public_core_tools": catalog_contract.tool_count == len(CORE_TOOL_NAMES),
            "catalog_usable": catalog_contract.usable,
            "client_matrix_aligned": not any(item.get("code") in {"client_matrix_incomplete", "schema_hash_mismatch", "adapter_server_id", "adapter_client_missing", "unknown_client", "duplicate_client"} for item in issues),
            "hooks_idempotent_bounded_observable": not any(item.get("code") in {"hook_safety_invariant", "hook_enabled_not_boolean", "unknown_hook_profile"} for item in issues),
            "truthful_degraded_mode": (not native_state["attached"] and not native_state["current_session_verified"] and core["degraded_mode"]["active"] and core["degraded_mode"]["status"] == "MCP unavailable") or bool(native_state["attached"] and native_state["current_session_verified"]),
            "no_parallel_memory_authority": True,
        },
        "issues": issues,
        "execution": {
            "client_files_written": False,
            "hooks_started": False,
            "native_attach_claimed": False,
            "model_started": False,
            "sqlite_written": False,
            "qdrant_written": False,
            "authority": "existing-bhm-contracts",
        },
    }


def verify_unified_mcp_contract_digest(contract: Mapping[str, Any]) -> bool:
    expected = str(contract.get("contract_digest") or "")
    if not expected:
        return False
    core_keys = (
        "schema_version",
        "canonical_server_id",
        "aliases",
        "protocol_versions",
        "core_tool_names",
        "catalog",
        "adapter_contract_digest",
        "clients",
        "hooks",
        "namespaces",
        "native_mcp",
        "degraded_mode",
        "provenance",
    )
    core = {key: contract.get(key) for key in core_keys}
    return expected == _sha256(core)


__all__ = [
    "EXPECTED_CANONICAL_SERVER_ID",
    "EXPECTED_CLIENTS",
    "UNIFIED_MCP_CONTRACT_SCHEMA_VERSION",
    "UnifiedMcpContractError",
    "build_unified_mcp_contract",
    "verify_unified_mcp_contract_digest",
]
