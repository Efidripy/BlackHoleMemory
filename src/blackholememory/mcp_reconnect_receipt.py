"""Bounded, deterministic MCP reconnect/lease recovery receipt.

The receipt is an operator-facing diagnosis, not a client-side reconnecter.  It
keeps the server lease, catalog/contract and runtime gates explicit while never
exposing session identifiers or credentials and never claiming native Codex
attachment without a live native tool call.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping


MCP_RECONNECT_RECEIPT_SCHEMA_VERSION = "bhm.mcp.reconnect-receipt.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_text(value: Any, maximum: int = 128) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text[:maximum] if text else None


def _bounded_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(max(0.0, min(number, 86_400.0)), 3)


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_mcp_reconnect_receipt(
    *,
    connected: Mapping[str, Any] | None,
    catalog: Mapping[str, Any] | None,
    runtime: Mapping[str, Any] | None,
    schema_drift: Mapping[str, Any] | None,
    rest_degraded: Mapping[str, Any] | None,
    http_sessions: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify recoverability without mutating runtime or client state."""

    connected = connected if isinstance(connected, Mapping) else {}
    catalog = catalog if isinstance(catalog, Mapping) else {}
    runtime = runtime if isinstance(runtime, Mapping) else {}
    schema_drift = schema_drift if isinstance(schema_drift, Mapping) else {}
    rest = rest_degraded if isinstance(rest_degraded, Mapping) else {}
    sessions = http_sessions if isinstance(http_sessions, Mapping) else {}

    connected_state = _bounded_text(connected.get("state"), 24) or "unknown"
    catalog_state = _bounded_text(catalog.get("state"), 24) or "unverified"
    runtime_state = _bounded_text(runtime.get("state"), 24) or "unknown"
    drift_state = _bounded_text(schema_drift.get("state"), 24) or "unverified"
    transport_ready = bool(rest.get("transport_ready"))
    rows = sessions.get("sessions") if isinstance(sessions.get("sessions"), list) else []
    rows = [row for row in rows if isinstance(row, Mapping)]
    lease_values = [
        value
        for row in rows
        for value in [_bounded_float(row.get("lease_remaining_seconds"))]
        if value is not None
    ]
    protocol_versions = sorted(
        {
            value
            for row in rows
            for value in [_bounded_text(row.get("protocol_version"), 32)]
            if value
        }
    )
    catalog_hashes = sorted(
        {
            value
            for row in rows
            for value in [_bounded_text(row.get("catalog_hash"), 128)]
            if value
        }
    )
    contract_digests = sorted(
        {
            value
            for row in rows
            for value in [_bounded_text(row.get("contract_digest"), 128)]
            if value
        }
    )

    expired_count = max(0, min(int(sessions.get("expired_count") or 0), 32))
    if drift_state == "detected" or any(row.get("contract_state") == "drifted" for row in rows):
        state = "drifted"
        status = "blocked"
        action = "contract_drift_review"
        gaps = ["contract_or_catalog_drift"]
    elif connected_state == "attached" and catalog_state == "ready" and runtime_state == "healthy":
        state = "attached"
        status = "pass"
        action = "reuse_session"
        gaps = ["native_client_attach_not_proven"]
    elif expired_count:
        state = "expired"
        status = "gap"
        action = "reinitialize"
        gaps = ["session_lease_expired_or_absent"]
    elif connected_state == "pending":
        state = "pending"
        status = "gap"
        action = "complete_initialize_and_catalog_probe"
        gaps = ["session_catalog_not_ready"]
    elif transport_ready and runtime_state == "healthy":
        state = "detached"
        status = "gap"
        action = "native_probe"
        gaps = ["no_live_session_lease"]
    else:
        state = "detached"
        status = "blocked"
        action = "runtime_restart_required"
        gaps = ["canonical_transport_or_runtime_unhealthy"]

    stable = {
        "schema_version": MCP_RECONNECT_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "state": state,
        "action": action,
        "gaps": sorted(set(gaps)),
        "transport_ready": transport_ready,
        "runtime_state": runtime_state,
        "catalog_state": catalog_state,
        "schema_drift_state": drift_state,
        "protocol_versions": protocol_versions,
        "catalog_hashes": catalog_hashes,
        "contract_digests": contract_digests,
        "lease": {
            "observed": bool(rows),
            "count": min(len(rows), 32),
            "expired_count": expired_count,
            "remaining_seconds_min": min(lease_values) if lease_values else None,
            "remaining_seconds_max": max(lease_values) if lease_values else None,
        },
        "native_client_attach_proven": False,
        "automatic_client_reconnect": False,
        "read_only": True,
        "writes_live_state": False,
    }
    return {
        **stable,
        "generated_at": _utc_now(),
        "deterministic_digest": _digest(stable),
    }


__all__ = ["MCP_RECONNECT_RECEIPT_SCHEMA_VERSION", "build_mcp_reconnect_receipt"]
