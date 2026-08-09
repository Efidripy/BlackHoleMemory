"""Bounded, read-only MCP status aggregation for operator panels.

The panel deliberately keeps four independent gates separate: configured
client sources, a live Streamable HTTP session, a catalog bound to that
session, and BHM runtime health/SLO. Legacy heartbeat/stdio state is ignored.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .mcp_surfaces import CORE_TOOL_NAMES
from .mcp_reconnect_receipt import build_mcp_reconnect_receipt


SCHEMA_VERSION = "bhm.mcp.panel.v1"
# The expanded CBM-compatible core catalog is the single source of truth.
# Keep the exported constant for compatibility with older panel consumers.
EXPECTED_CORE_TOOL_COUNT = len(CORE_TOOL_NAMES)
MAX_SOURCES = 8
MAX_CLIENT_VERSIONS = 16
MAX_ERROR_LENGTH = 180
MAX_GENERATIONS = 8
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_int(value: Any, *, maximum: int = 1_000_000) -> int:
    try:
        return min(max(int(value), 0), maximum)
    except (TypeError, ValueError):
        return 0


def _bounded_seconds(value: Any, *, maximum: int = 86_400) -> int:
    try:
        return min(max(int(round(float(value))), 0), maximum)
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_label(value: Any, *, fallback: str = "unknown", maximum: int = 128) -> str:
    candidate = str(value or "").strip()
    if len(candidate) > maximum or not _SAFE_LABEL_RE.fullmatch(candidate):
        return fallback
    return candidate


def _safe_reason(value: Any) -> str:
    reason = " ".join(str(value or "").split()).strip()
    return reason[:MAX_ERROR_LENGTH] or "unknown"


def _read_manifest(path: Path) -> Mapping[str, Any] | None:
    try:
        path = assert_safe_path(path)
        if not path.is_file() or path.stat().st_size > 128 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _resolve_target(raw: Any, *, repo_root: Path, user_root: Path | None) -> Path | None:
    target = str(raw or "").strip()
    if not target:
        return None
    workspace_root = repo_root.parent.parent
    replacements = {
        "<repo>": repo_root,
        "<user>": user_root,
        "<workspace>": workspace_root,
    }
    for marker, root in replacements.items():
        if marker in target:
            if root is None:
                return None
            target = target.replace(marker, str(root))
    return Path(target).expanduser()


def _entry_present(path: Path | None, *, file_format: str, server_id: str) -> tuple[bool, bool]:
    """Return (target_present, managed_entry_present) without exposing content."""

    if path is None:
        return False, False
    try:
        path = assert_safe_path(path)
        target_present = path.is_file()
        if not target_present or path.stat().st_size > 256 * 1024:
            return target_present, False
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False, False

    if file_format.casefold() == "toml":
        section = re.compile(rf"(?m)^\s*\[mcp_servers\.{re.escape(server_id)}\]\s*$")
        return True, bool(section.search(raw))

    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        return True, False
    servers = payload.get("mcpServers") if isinstance(payload, Mapping) else None
    return True, isinstance(servers, Mapping) and server_id in servers


def load_configured_sources(
    repo_root: Path,
    *,
    user_root: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Read only bounded client-registration presence from the adapter manifest."""

    try:
        root = assert_safe_path(Path(repo_root).expanduser(), reject_hardlink_target=False)
        if not root.is_dir():
            raise OSError("repository root is not a directory")
        manifest = assert_safe_path(Path(manifest_path or root / "config" / "mcp-registration.json").expanduser())
    except OSError:
        return {
            "schema_version": "bhm.mcp.configured-sources.v1",
            "status": "unavailable",
            "manifest_present": False,
            "source_count": 0,
            "configured_count": 0,
            "sources": [],
            "read_only": True,
            "writes_live_state": False,
        }
    payload = _read_manifest(manifest)
    contract = payload.get("adapter_contract") if isinstance(payload, Mapping) else None
    clients = contract.get("clients") if isinstance(contract, Mapping) else None
    records: list[dict[str, Any]] = []

    if isinstance(clients, Mapping):
        for client_name, raw in sorted(clients.items(), key=lambda item: str(item[0])):
            if not isinstance(raw, Mapping):
                continue
            client = _safe_label(client_name, fallback="unknown", maximum=32)
            file_format = _safe_label(raw.get("format"), fallback="unknown", maximum=12)
            server_id = _safe_label(raw.get("server_id"), fallback="unknown", maximum=64)
            target_path = _resolve_target(raw.get("target"), repo_root=root, user_root=user_root)
            target_present, entry_present = _entry_present(
                target_path,
                file_format=file_format,
                server_id=server_id,
            )
            records.append(
                {
                    "client": client,
                    "format": file_format,
                    "server_id": server_id,
                    "managed_scope": _safe_label(raw.get("managed_scope"), fallback="unknown"),
                    "reload_action": _safe_label(raw.get("reload_action"), fallback="unknown"),
                    "target_present": target_present,
                    "entry_present": entry_present,
                    "configured": entry_present,
                }
            )

    records = records[:MAX_SOURCES]
    configured_count = sum(1 for item in records if item["configured"])
    source_count = len(records)
    if payload is None or not manifest.is_file():
        status = "unavailable"
    elif source_count and configured_count == source_count:
        status = "configured"
    elif configured_count:
        status = "partial"
    else:
        status = "missing"
    return {
        "schema_version": "bhm.mcp.configured-sources.v1",
        "status": status,
        "manifest_present": bool(payload is not None and manifest.is_file()),
        "source_count": source_count,
        "configured_count": configured_count,
        "sources": records,
        "read_only": True,
        "writes_live_state": False,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _active_leases(attach: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = attach.get("leases")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping) and row.get("status") == "attached"][:MAX_SOURCES]


def _active_http_sessions(http_sessions: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = http_sessions.get("sessions")
    if not isinstance(rows, list):
        return []
    result: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("state") not in {"catalog_ready", "healthy"}:
            continue
        catalog_hash = str(row.get("catalog_hash") or "").strip()
        result.append(
            {
                "version": row.get("client_version"),
                "surface": "core",
                "transport": "streamable_http",
                "transport_generation": f"http-{catalog_hash[:24]}" if catalog_hash else "",
                "catalog_hash": catalog_hash,
                "contract_digest": str(row.get("contract_digest") or "").strip()[:128],
                "contract_state": str(row.get("contract_state") or "unverified")[:16],
                "tool_count": (
                    _bounded_int(row.get("tool_count"), maximum=128)
                    if isinstance(row.get("tool_count"), int)
                    else None
                ),
                "lease_remaining_seconds": _bounded_seconds(row.get("lease_remaining_seconds")),
            }
        )
    return result[:MAX_SOURCES]


def _client_versions(leases: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    for lease in leases:
        version = _safe_label(lease.get("version"), fallback="unknown")
        surface = _safe_label(lease.get("surface"), fallback="unknown")
        counts[(version, surface)] += 1
    return [
        {"version": version, "surface": surface, "count": count}
        for (version, surface), count in sorted(counts.items())[:MAX_CLIENT_VERSIONS]
    ]


def _runtime_state(runtime: Mapping[str, Any]) -> dict[str, Any]:
    ready = _mapping(runtime.get("ready"))
    cutover = _mapping(runtime.get("cutover"))
    slo = _mapping(runtime.get("slo"))
    ready_ok = ready.get("ok") is True
    cutover_ok = cutover.get("ok") is True or cutover.get("required_ok") is True
    slo_ok = slo.get("ok") is True and slo.get("status") == "healthy"
    provider_ready = bool(
        _mapping(slo.get("observed")).get("provider_ready")
        or _mapping(ready.get("provider_warmup")).get("ready")
    )
    available = bool(ready or cutover or slo)
    state = "healthy" if ready_ok and cutover_ok and slo_ok else "degraded" if available else "unavailable"
    return {
        "state": state,
        "ready": ready_ok,
        "cutover": cutover_ok,
        "slo": str(slo.get("status") or "unavailable")[:24],
        "provider_ready": provider_ready,
    }


def _latest_connection_error(connection: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = connection.get("connections")
    if not isinstance(rows, list):
        return None
    candidates = [
        row
        for row in rows
        if isinstance(row, Mapping) and str(row.get("state")) in {"failed", "degraded", "reconnecting"}
    ]
    candidates.sort(key=lambda row: str(_mapping(row.get("timestamps")).get("updated_at") or ""), reverse=True)
    if not candidates:
        return None
    row = candidates[0]
    state = _safe_label(row.get("state"), fallback="unknown", maximum=24)
    timestamps = _mapping(row.get("timestamps"))
    return {
        "state": state,
        "reason": _safe_reason(row.get("reason")),
        "at": _safe_reason(timestamps.get("updated_at")),
    }


def _latest_reconnect(telemetry: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = telemetry.get("recent_events")
    if not isinstance(rows, list):
        return None
    candidates = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and (str(row.get("stage")) == "reconnect" or str(row.get("reconnect_reason")) not in {"", "none"})
    ]
    if not candidates:
        return None
    row = candidates[-1]
    return {
        "reason": _safe_label(row.get("reconnect_reason"), fallback="unknown", maximum=64),
        "error_code": _safe_label(row.get("error_code"), fallback="unknown", maximum=64),
        "at": _safe_reason(row.get("at")),
    }


def build_mcp_panel_snapshot(
    *,
    configured: Mapping[str, Any] | None,
    attach: Mapping[str, Any] | None,
    connection: Mapping[str, Any] | None,
    telemetry: Mapping[str, Any] | None,
    runtime: Mapping[str, Any] | None,
    http_sessions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the UI contract from already collected, bounded runtime snapshots."""

    configured_data = _mapping(configured)
    http_data = _mapping(http_sessions)
    connection_data = _mapping(connection)
    telemetry_data = _mapping(telemetry)
    leases = _active_http_sessions(http_data)[:MAX_SOURCES]
    attached_count = min(_bounded_int(http_data.get("attached_count"), maximum=MAX_SOURCES), MAX_SOURCES)
    pending_count = min(_bounded_int(http_data.get("pending_count"), maximum=MAX_SOURCES), MAX_SOURCES)
    if attached_count != len(leases):
        attached_count = len(leases)
    attach_status = "attached" if attached_count else str(http_data.get("status") or "unknown")
    connected_state = (
        "attached" if attached_count else "pending" if pending_count or attach_status == "pending" else "detached"
    )
    client_versions = _client_versions(leases)

    catalog_pairs: set[tuple[str, str]] = set()
    catalog_missing = False
    for lease in leases:
        generation = str(lease.get("transport_generation") or "").strip()
        catalog_hash = str(lease.get("catalog_hash") or "").strip()
        if not generation or not catalog_hash:
            catalog_missing = True
        else:
            catalog_pairs.add((generation[:128], catalog_hash[:128]))
    if len(catalog_pairs) > 1:
        catalog_state = "unavailable"
    elif catalog_pairs and not catalog_missing:
        catalog_state = "ready"
    elif pending_count:
        catalog_state = "pending"
    else:
        catalog_state = "unverified"
    catalog_generation = sorted(catalog_pairs)[0][0] if len(catalog_pairs) == 1 else None
    catalog_hash = sorted(catalog_pairs)[0][1] if len(catalog_pairs) == 1 else None
    contract_digests = {
        str(lease.get("contract_digest") or "").strip()[:128]
        for lease in leases
        if str(lease.get("contract_digest") or "").strip()
    }
    contract_digest_leases = sum(
        1 for lease in leases if str(lease.get("contract_digest") or "").strip()
    )
    contract_states = {
        str(lease.get("contract_state") or "").strip()[:16]
        for lease in leases
        if str(lease.get("contract_state") or "").strip()
    }
    contract_state_leases = sum(
        1 for lease in leases if str(lease.get("contract_state") or "").strip()
    )
    aggregate_contract_digest = (
        next(iter(contract_digests))
        if len(contract_digests) == 1 and contract_digest_leases == len(leases)
        else None
    )
    aggregate_contract_state = (
        next(iter(contract_states))
        if len(contract_states) == 1 and contract_state_leases == len(leases)
        else "unverified"
    )
    observed_tool_counts = {
        int(lease["tool_count"])
        for lease in leases
        if isinstance(lease.get("tool_count"), int)
    }
    observed_tool_count_leases = sum(
        1 for lease in leases if isinstance(lease.get("tool_count"), int)
    )
    # A detached transport has observed zero live tools. Keep ``None`` only
    # for an attached lease whose catalog is present but omits its count; that
    # distinction lets the panel satisfy the ready-detached contract without
    # turning an unverified session into a false catalog PASS.
    observed_tool_count = (
        0
        if not leases
        else next(iter(observed_tool_counts))
        if len(observed_tool_counts) == 1 and observed_tool_count_leases == len(leases)
        else None
    )
    coverage_state = (
        "pass"
        if observed_tool_count == EXPECTED_CORE_TOOL_COUNT and catalog_state == "ready"
        else "mismatch"
        if observed_tool_count is not None and catalog_state == "ready"
        else "unverified"
    )
    coverage_missing = (
        max(EXPECTED_CORE_TOOL_COUNT - observed_tool_count, 0)
        if observed_tool_count is not None and leases
        else None
    )
    coverage_extra = (
        max(observed_tool_count - EXPECTED_CORE_TOOL_COUNT, 0)
        if observed_tool_count is not None and leases
        else None
    )
    catalog_coverage = {
        "schema_version": "bhm.mcp.catalog-coverage.v1",
        "expected": EXPECTED_CORE_TOOL_COUNT,
        "observed": observed_tool_count,
        "missing": coverage_missing,
        "extra": coverage_extra,
        "state": coverage_state,
        "contract_digest": aggregate_contract_digest,
        "read_only": True,
        "writes_live_state": False,
    }
    catalog = {
        "state": catalog_state,
        "expected_tool_count": EXPECTED_CORE_TOOL_COUNT,
        "observed_tool_count": observed_tool_count,
        "generation": catalog_generation,
        "catalog_hash": catalog_hash,
        "generation_count": len(catalog_pairs),
        "contract_digest": aggregate_contract_digest,
        "contract_state": aggregate_contract_state,
    }

    if not leases:
        schema_drift_state = "unverified"
        schema_drift_reason = "no_live_native_catalog"
    elif len(catalog_pairs) > 1:
        schema_drift_state = "detected"
        schema_drift_reason = "live_catalog_generations_mismatch"
    elif len(contract_digests) > 1 or len(contract_states) > 1:
        schema_drift_state = "detected"
        schema_drift_reason = "live_contract_digests_mismatch"
    elif len(leases) > 1 and (
        contract_digest_leases != len(leases) or contract_state_leases != len(leases)
    ):
        schema_drift_state = "unverified"
        schema_drift_reason = "contract_digest_not_bound"
    elif catalog_missing:
        schema_drift_state = "unverified"
        schema_drift_reason = "catalog_generation_not_bound"
    else:
        schema_drift_state = "none"
        schema_drift_reason = "single_live_catalog_generation"
    schema_drift = {
        "state": schema_drift_state,
        "reason_code": schema_drift_reason,
        "generation_count": len(catalog_pairs),
    }

    runtime_state = _runtime_state(_mapping(runtime))
    configured_state = str(configured_data.get("status") or "unavailable")
    configured_ok = configured_state == "configured" and _bounded_int(configured_data.get("configured_count")) > 0
    streamable_http_ready = (
        http_data.get("schema_version") == "bhm.mcp.streamable-http.v1"
        and http_data.get("authoritative_source") == "streamable_http_sessions"
    )
    runtime_lease_live = bool(leases)
    transport_ready = streamable_http_ready or runtime_lease_live
    rest_degraded = not runtime_lease_live
    if runtime_lease_live:
        rest_status = "native MCP live; current session unverified"
        rest_reason = "current_session_unverified"
        recovery_action = "native MCP session is live; verify this client with a native BHM tool call"
    elif streamable_http_ready:
        rest_status = "native MCP transport ready; session idle or detached"
        rest_reason = "streamable_http_idle_or_detached"
        if runtime_state["state"] == "healthy":
            recovery_action = (
                "invoke a native BHM tool to establish or recover the Streamable HTTP session; "
                "reload only if the native probe fails while runtime is healthy"
            )
        else:
            recovery_action = (
                "repair runtime/SLO, then invoke a native BHM tool; reload only after a healthy native probe fails"
            )
    else:
        rest_status = "MCP unavailable"
        rest_reason = "native_mcp_transport_unavailable"
        recovery_action = (
            "start or repair the canonical BHM transport and re-probe; reload only after runtime/config repair"
        )
    rest_degraded_data = {
        "status": rest_status,
        "degraded": rest_degraded,
        "mcp_available": runtime_lease_live,
        "attached": runtime_lease_live,
        "current_session_verified": False,
        "runtime_lease_live": runtime_lease_live,
        "transport_ready": transport_ready,
        "streamable_http_ready": streamable_http_ready,
        "reason_code": rest_reason,
        "recovery_action": recovery_action,
    }

    healthy_gates = {
        "configured": configured_ok,
        "connected": connected_state == "attached",
        "catalog": catalog_state == "ready",
        "catalog_coverage": coverage_state == "pass",
        "runtime": runtime_state["state"] == "healthy",
        "schema_drift": schema_drift_state == "none",
        "rest_degraded": not rest_degraded,
    }
    reconnect_receipt = build_mcp_reconnect_receipt(
        connected={
            "state": connected_state,
            "attached_count": attached_count,
            "pending_count": pending_count,
        },
        catalog=catalog,
        runtime=runtime_state,
        schema_drift=schema_drift,
        rest_degraded=rest_degraded_data,
        http_sessions=http_data,
    )
    if all(healthy_gates.values()):
        overall_state = "healthy"
        overall_reason = "all_mcp_health_gates_pass"
    elif runtime_state["state"] == "unavailable":
        overall_state = "unavailable"
        overall_reason = "runtime_unavailable"
    elif schema_drift_state == "detected":
        overall_state = "degraded"
        overall_reason = schema_drift_reason
    elif rest_degraded and streamable_http_ready and runtime_state["state"] == "healthy":
        overall_state = "warning"
        overall_reason = "streamable_http_ready_session_idle_or_detached"
    elif rest_degraded and streamable_http_ready:
        overall_state = "degraded"
        overall_reason = "runtime_degraded_streamable_http_ready_session_idle"
    elif rest_degraded:
        overall_state = "degraded"
        overall_reason = "native_mcp_transport_unavailable_rest_bridge_active"
    elif catalog_state != "ready":
        overall_state = "warning"
        overall_reason = "catalog_not_ready"
    elif not configured_ok:
        overall_state = "warning"
        overall_reason = "mcp_sources_not_configured"
    else:
        overall_state = "degraded"
        overall_reason = "mcp_health_gate_failed"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "read_only": True,
        "writes_live_state": False,
        "bounded": True,
        "configured": {
            "state": configured_state,
            "source_count": _bounded_int(configured_data.get("source_count"), maximum=MAX_SOURCES),
            "configured_count": _bounded_int(configured_data.get("configured_count"), maximum=MAX_SOURCES),
            "sources": list(configured_data.get("sources") or [])[:MAX_SOURCES],
        },
        "connected": {
            "state": connected_state,
            "attached_count": attached_count,
            "pending_count": pending_count,
            "client_versions": client_versions,
            "transports": sorted({str(item.get("transport") or "streamable_http") for item in leases}),
            "protocol_state": _safe_label(connection_data.get("status"), fallback="unknown", maximum=24),
        },
        "catalog": catalog,
        "catalog_coverage": catalog_coverage,
        "runtime": runtime_state,
        "errors": {
            "last_error": _latest_connection_error(connection_data),
            "last_reconnect": _latest_reconnect(telemetry_data),
        },
        "schema_drift": schema_drift,
        "reconnect_receipt": reconnect_receipt,
        "rest_degraded": rest_degraded_data,
        "overall": {
            "state": overall_state,
            "reason_code": overall_reason,
            "false_green_prevented": overall_state != "healthy",
            "gates": healthy_gates,
        },
    }


__all__ = [
    "EXPECTED_CORE_TOOL_COUNT",
    "SCHEMA_VERSION",
    "build_mcp_panel_snapshot",
    "load_configured_sources",
]
