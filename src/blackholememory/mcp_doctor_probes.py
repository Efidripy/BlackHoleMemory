"""Bounded read-only MCP Doctor probe implementations for status and health contracts."""

from __future__ import annotations

from collections import Counter
import json
import time
from typing import Any, Mapping
import urllib.error
import urllib.request

from .local_endpoint_policy import (
    LocalEndpointError,
    open_local_url,
    read_bounded_response,
)
from .mcp_catalog_contract import CatalogContractError, build_catalog_contract
from .mcp_protocol_contract import CURRENT_PROTOCOL_VERSION
from .version_manifest import PACKAGE_VERSION
from .mcp_doctor_base import (
    DoctorConfig,
    MAX_FINGERPRINTS,
    MAX_HTTP_BYTES,
    MAX_ISSUES,
    _bhm_request_headers,
    _bounded_float,
    _bounded_int,
    _run_json_script,
    _safe_issue_code,
)


def _configured_sources(config: DoctorConfig) -> dict[str, Any]:
    script = config.repo_root / "scripts" / "generate-bhm-mcp-adapters.py"
    payload, returncode = _run_json_script(
        script,
        [
            "--check",
            "--json",
            "--manifest",
            str(config.resolved_manifest),
            "--repo-root",
            str(config.repo_root),
        ],
        config=config,
    )
    records: list[dict[str, Any]] = []
    for item in (
        payload.get("clients") if isinstance(payload.get("clients"), list) else []
    ):
        if not isinstance(item, Mapping):
            continue
        records.append(
            {
                "client": str(item.get("client") or "unknown")[:32],
                "format": str(item.get("format") or "unknown")[:12],
                "server_id": str(item.get("server_id") or "unknown")[:32],
                "configured": bool(item.get("exists")),
                "aligned": bool(item.get("ok")),
                "issue_codes": sorted(
                    {_safe_issue_code(value) for value in (item.get("issues") or [])}
                )[:8],
            }
        )
    records.sort(key=lambda item: item["client"])
    aligned = bool(
        payload.get("ok") is True
        and records
        and all(item["aligned"] for item in records)
    )
    return {
        "status": "aligned" if aligned else "drift_or_unavailable",
        "manifest_present": config.resolved_manifest.is_file(),
        "source_count": len(records),
        "configured_count": sum(1 for item in records if item["configured"]),
        "aligned_count": sum(1 for item in records if item["aligned"]),
        "sources": records[:8],
        "validator_returncode": returncode,
        "writes_live_state": bool(payload.get("writes_live_state", False)),
    }


def _duplicate_fingerprints(config: DoctorConfig) -> dict[str, Any]:
    script = config.repo_root / "scripts" / "validate-bhm-mcp-registration.py"
    payload, returncode = _run_json_script(
        script,
        [
            "--live",
            "--contract",
            str(config.resolved_manifest),
            "--codex-config",
            str(config.resolved_codex_config),
        ],
        config=config,
    )
    issues = [
        item for item in (payload.get("issues") or []) if isinstance(item, Mapping)
    ]
    issue_codes = Counter(_safe_issue_code(item.get("code")) for item in issues)
    duplicate_fingerprints = sorted(
        {
            str(fingerprint)[:64]
            for item in issues
            if item.get("code") == "duplicate_fingerprint"
            for fingerprint in (item.get("fingerprints") or [])
            if len(str(fingerprint)) == 64
        }
    )[:MAX_FINGERPRINTS]
    active_conflict_codes = {
        "canonical_registration_count",
        "canonical_fingerprint_drift",
        "unrecognized_bhm_surface",
        "alias_registration",
        "duplicate_fingerprint",
    }
    active_conflict = any(code in active_conflict_codes for code in issue_codes)
    if not issues:
        status = "clean"
    elif active_conflict:
        status = "active_conflict"
    else:
        status = "active_conflict"
    return {
        "status": status,
        "fail_closed": bool(payload.get("fail_closed")),
        "registration_count": _bounded_int(
            payload.get("registration_count"), maximum=256
        ),
        "source_count": _bounded_int(len(payload.get("sources") or []), maximum=64),
        "issue_count": min(len(issues), MAX_ISSUES),
        "issue_counts": dict(list(sorted(issue_codes.items()))[:MAX_ISSUES]),
        "duplicate_fingerprint_count": len(duplicate_fingerprints),
        "duplicate_fingerprints": duplicate_fingerprints,
        "alias_registration_count": _bounded_int(
            issue_codes.get("alias_registration", 0), maximum=64
        ),
        "active_conflict": active_conflict,
        "validator_returncode": returncode,
        "writes_live_state": bool(payload.get("writes_live_state", False)),
    }


def _get_json(
    base_url: str, path: str, *, timeout_seconds: float
) -> tuple[dict[str, Any] | None, str | None]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers=_bhm_request_headers(path, accept="application/json"),
    )
    try:
        with open_local_url(
            request, timeout=max(min(timeout_seconds, 15.0), 0.2)
        ) as response:
            try:
                raw = read_bounded_response(response, limit=MAX_HTTP_BYTES)
            except LocalEndpointError:
                return None, "response_too_large"
        if len(raw) > MAX_HTTP_BYTES:
            return None, "response_too_large"
        payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"http_{int(exc.code)}"
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return None, "unreachable"
    if not isinstance(payload, dict):
        return None, "non_object_response"
    return payload, None


def _runtime_snapshot(config: DoctorConfig) -> dict[str, Any]:
    ready, ready_error = _get_json(
        config.base_url, "/health/ready", timeout_seconds=config.timeout_seconds
    )
    cutover, cutover_error = _get_json(
        config.base_url, "/health/cutover", timeout_seconds=config.timeout_seconds
    )
    slo, slo_error = _get_json(
        config.base_url, "/bhm/health/slo", timeout_seconds=config.timeout_seconds
    )
    memory_store = ready.get("memory_store") if isinstance(ready, Mapping) else {}
    mem0 = ready.get("mem0") if isinstance(ready, Mapping) else {}
    observed = slo.get("observed") if isinstance(slo, Mapping) else {}
    outbox = observed.get("outbox") if isinstance(observed, Mapping) else {}
    reachable = ready is not None or cutover is not None or slo is not None
    ready_ok = bool(ready and ready.get("ok") is True)
    cutover_ok = bool(cutover and cutover.get("ok") is True)
    slo_ok = bool(slo and slo.get("status") == "healthy" and slo.get("ok") is True)
    return {
        "status": "healthy"
        if ready_ok and cutover_ok and slo_ok
        else "degraded"
        if reachable
        else "unreachable",
        "reachable": reachable,
        "ready": ready_ok,
        "cutover": cutover_ok,
        "slo": str(slo.get("status") if isinstance(slo, Mapping) else "unavailable")[
            :24
        ],
        "slo_ok": slo_ok,
        "memory_store": str(
            (memory_store or {}).get("backend")
            or (mem0 or {}).get("memory_store_mode")
            or "unknown"
        )[:48],
        "projection_pending": _bounded_int(
            (observed or {}).get("projection_pending"), maximum=100_000
        ),
        "projection_failed": _bounded_int(
            (observed or {}).get("projection_failed"), maximum=100_000
        ),
        "outbox_pending": _bounded_int((outbox or {}).get("pending"), maximum=100_000),
        "outbox_failed": _bounded_int((outbox or {}).get("failed"), maximum=100_000),
        "error_codes": sorted(
            code for code in (ready_error, cutover_error, slo_error) if code
        )[:8],
    }


def _http_mcp_request(
    url: str,
    message: dict[str, Any] | None,
    *,
    timeout_seconds: float,
    session_id: str = "",
    method: str = "POST",
) -> tuple[int, dict[str, Any], Mapping[str, str]]:
    headers = _bhm_request_headers("/mcp", accept="application/json, text/event-stream")
    data = None
    if message is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if session_id:
        headers["Mcp-Session-Id"] = session_id
        headers["MCP-Protocol-Version"] = CURRENT_PROTOCOL_VERSION
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with open_local_url(
            request, timeout=max(min(timeout_seconds, 15.0), 0.2)
        ) as response:
            try:
                raw = read_bounded_response(response, limit=MAX_HTTP_BYTES)
            except LocalEndpointError as exc:
                raise ValueError("response_too_large") from exc
            status = int(response.status)
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_HTTP_BYTES + 1)
        status = int(exc.code)
        response_headers = dict(exc.headers.items())
    if len(raw) > MAX_HTTP_BYTES:
        raise ValueError("response_too_large")
    if not raw:
        return status, {}, response_headers
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("protocol_non_object_response")
    return status, payload, response_headers


def _streamable_http_protocol_probe(
    config: DoctorConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    endpoint = f"{config.base_url.rstrip('/')}/mcp"
    started = time.perf_counter()
    initialize: dict[str, Any] = {}
    catalog_response: dict[str, Any] = {}
    session_id = ""
    initialize_ok = False
    catalog_ok = False
    shutdown_ok = False
    error_code = "none"
    try:
        status, initialize, response_headers = _http_mcp_request(
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": CURRENT_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "BHM MCP Doctor",
                        "version": PACKAGE_VERSION,
                    },
                },
            },
            timeout_seconds=config.timeout_seconds,
        )
        session_id = next(
            (
                value
                for key, value in response_headers.items()
                if key.casefold() == "mcp-session-id"
            ),
            "",
        )
        initialize_ok = (
            status == 200
            and bool(session_id)
            and "result" in initialize
            and not initialize.get("error")
        )
        if not initialize_ok:
            raise ConnectionError("http_initialize_failed")
        notification_status, _, _ = _http_mcp_request(
            endpoint,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            timeout_seconds=config.timeout_seconds,
            session_id=session_id,
        )
        if notification_status != 202:
            raise ConnectionError("http_initialized_notification_failed")
        catalog_status, catalog_response, _ = _http_mcp_request(
            endpoint,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            timeout_seconds=config.timeout_seconds,
            session_id=session_id,
        )
        catalog_ok = (
            catalog_status == 200
            and "result" in catalog_response
            and not catalog_response.get("error")
        )
        delete_status, _, _ = _http_mcp_request(
            endpoint,
            None,
            timeout_seconds=config.timeout_seconds,
            session_id=session_id,
            method="DELETE",
        )
        shutdown_ok = delete_status in {200, 202, 204}
    except TimeoutError:
        error_code = "protocol_timeout"
    except (OSError, ConnectionError, ValueError, json.JSONDecodeError):
        error_code = "streamable_http_unavailable"
    contract: dict[str, Any] = {}
    try:
        contract = build_catalog_contract(initialize, catalog_response).as_dict()
    except CatalogContractError:
        if error_code == "none":
            error_code = "catalog_contract_invalid"
    protocol_ok = bool(
        initialize_ok and catalog_ok and contract.get("usable") is True and shutdown_ok
    )
    if protocol_ok:
        error_code = "none"
    return (
        {
            "attempted": True,
            "connected": initialize_ok or catalog_ok,
            "transport": "streamable_http",
            "returncode": 0 if protocol_ok else None,
            "duration_ms": _bounded_float((time.perf_counter() - started) * 1000.0),
            "readiness": {
                "api": "ready" if initialize_ok else "unknown",
                "broker": "connected" if initialize_ok else "unknown",
                "protocol": "ready" if initialize_ok else "unknown",
                "catalog": "ready"
                if catalog_ok and contract.get("usable") is True
                else "unknown",
            },
            "readiness_inferred": False,
            "error_code": error_code,
            "lease_heartbeat_disabled": False,
            "ephemeral_state_only": True,
        },
        {
            "ok": protocol_ok,
            "initialize_ok": initialize_ok,
            "catalog_ok": catalog_ok,
            "shutdown_ok": shutdown_ok,
            "protocol_version": str(
                ((initialize.get("result") or {}).get("protocolVersion")) or ""
            )[:32],
            "catalog": {
                "usable": bool(contract.get("usable")),
                "tool_count": _bounded_int(contract.get("tool_count"), maximum=128),
                "schema_hash": str(contract.get("schema_hash") or "")[:64],
                "generation": str(contract.get("generation") or "")[:64],
            },
            "returncode": 0 if protocol_ok else None,
            "transport": "streamable_http",
        },
    )


def _protocol_probe(config: DoctorConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    return _streamable_http_protocol_probe(config)


def _lease_snapshot(config: DoctorConfig) -> dict[str, Any]:
    http_payload, http_error = _get_json(
        config.base_url, "/bhm/mcp/http/status", timeout_seconds=config.timeout_seconds
    )
    http_sessions = (
        http_payload.get("sessions") if isinstance(http_payload, Mapping) else None
    )
    http_sessions = http_sessions if isinstance(http_sessions, Mapping) else {}
    if http_payload is None:
        return {
            "status": "unavailable",
            "attached_count": 0,
            "pending_count": 0,
            "expired_count": 0,
            "active_count": 0,
            "max_leases": 0,
            "authoritative_source": "streamable_http_sessions",
            "transports": {"streamable_http_attached": 0},
            "error_code": http_error or "unavailable",
        }
    attached = _bounded_int(http_sessions.get("attached_count"), maximum=64)
    pending = _bounded_int(http_sessions.get("pending_count"), maximum=64)
    return {
        "status": "attached" if attached else ("pending" if pending else "detached"),
        "attached_count": attached,
        "pending_count": pending,
        "expired_count": 0,
        "active_count": attached + pending,
        "max_leases": _bounded_int(http_sessions.get("max_sessions"), maximum=64),
        "authoritative_source": "streamable_http_sessions",
        "transports": {"streamable_http_attached": attached},
        "error_code": None,
    }


def _connection_snapshot(config: DoctorConfig) -> dict[str, Any]:
    payload, error = _get_json(
        config.base_url, "/bhm/mcp/http/status", timeout_seconds=config.timeout_seconds
    )
    if payload is None:
        return {
            "status": "unavailable",
            "connection_count": 0,
            "state_counts": {},
            "error_code": error,
        }
    sessions = (
        payload.get("sessions") if isinstance(payload.get("sessions"), Mapping) else {}
    )
    attached = _bounded_int(sessions.get("attached_count"), maximum=64)
    state_counts = {
        "attached": attached,
        "detached": max(
            0, _bounded_int(sessions.get("total_sessions"), maximum=64) - attached
        ),
    }
    return {
        "status": "attached" if attached else "detached",
        "connection_count": _bounded_int(sessions.get("total_sessions"), maximum=64),
        "state_counts": state_counts,
        "authoritative_source": "streamable_http_sessions",
        "error_code": None,
    }


def _ownership_snapshot(config: DoctorConfig) -> dict[str, Any]:
    return {
        "status": "retired",
        "record_count": 0,
        "valid_record_count": 0,
        "invalid_record_count": 0,
        "proofed_count": 0,
        "orphaned_count": 0,
        "scanned_process_count": 0,
        "cleanup_scope": "none",
        "broad_process_kill": False,
        "read_only_preview": True,
        "retired": True,
        "error_code": "legacy_stdio_retired",
    }
