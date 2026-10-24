"""Shared REST/MCP error taxonomy contract.

The HTTP and JSON-RPC surfaces keep their native wire envelopes, but expose a
single bounded semantic class so clients can handle equivalent failures without
parsing free-form messages.  This module is metadata-only; it never mutates
SQLite, Qdrant, or runtime state.
"""

from __future__ import annotations

from typing import Any, Mapping


ERROR_TAXONOMY_SCHEMA_VERSION = "bhm.error-taxonomy.v1"

# JSON-RPC standard errors plus the two bounded BHM capability/timeout errors
# already used by the gateway.
JSONRPC_ERROR_CLASSES: dict[int, str] = {
    -32600: "invalid_request",
    -32601: "method_not_found",
    -32602: "invalid_params",
    -32603: "internal_error",
    -32003: "admin_capability_required",
    -32004: "timeout",
}

# HTTP status classes are the fallback for REST responses whose detail is a
# legacy string.  Structured BHM responses still retain their exact detail
# code/error value on the wire.
REST_STATUS_CLASSES: dict[int, str] = {
    400: "invalid_request",
    401: "auth_required",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    411: "length_required",
    413: "payload_too_large",
    422: "validation_failed",
    429: "rate_limited",
    500: "internal_error",
    501: "not_implemented",
    503: "not_ready",
    504: "timeout",
}


def classify_jsonrpc_error(code: int) -> str:
    """Return the stable semantic class for a JSON-RPC error code."""

    return JSONRPC_ERROR_CLASSES.get(int(code), "internal_error")


def classify_rest_error(status_code: int, detail: Any = None) -> str:
    """Classify a REST error without discarding the native detail payload."""

    if isinstance(detail, Mapping):
        for key in ("code", "error"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:120]
    return REST_STATUS_CLASSES.get(int(status_code), f"http_{int(status_code)}")


def error_contract_snapshot() -> dict[str, Any]:
    """Return the JSON-safe shared contract published by both surfaces."""

    return {
        "schema_version": ERROR_TAXONOMY_SCHEMA_VERSION,
        "rest": {
            "status_classes": {str(status): value for status, value in sorted(REST_STATUS_CLASSES.items())},
            "structured_detail_precedence": ["code", "error"],
            "legacy_fallback": "http_<status>",
        },
        "mcp": {
            "jsonrpc_classes": {str(code): value for code, value in sorted(JSONRPC_ERROR_CLASSES.items())},
            "error_data_field": "bhm_error_code",
        },
    }
