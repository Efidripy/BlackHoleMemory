"""Versioned MCP protocol conformance contract for the BHM broker."""

from __future__ import annotations

from typing import Any, Mapping

from .error_taxonomy import error_contract_snapshot


PROTOCOL_CONTRACT_SCHEMA_VERSION = "bhm.mcp.protocol-conformance.v1"
CURRENT_PROTOCOL_VERSION = "2025-06-18"
LEGACY_PROTOCOL_VERSIONS = ("2024-11-05",)
SUPPORTED_PROTOCOL_VERSIONS = (CURRENT_PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS)

JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602


class ProtocolContractError(ValueError):
    """A request cannot be accepted by the bounded BHM protocol contract."""

    def __init__(self, message: str, *, code: int = JSONRPC_INVALID_PARAMS) -> None:
        super().__init__(message)
        self.code = int(code)


def negotiate_protocol_version(value: Any) -> str:
    """Return a supported MCP protocol version or fail closed."""

    if not isinstance(value, str) or not value.strip():
        raise ProtocolContractError("initialize requires a non-empty protocolVersion")
    requested = value.strip()
    if requested not in SUPPORTED_PROTOCOL_VERSIONS:
        supported = ", ".join(SUPPORTED_PROTOCOL_VERSIONS)
        raise ProtocolContractError(
            f"Unsupported MCP protocol version: {requested}; supported: {supported}",
        )
    return requested


def initialize_capabilities() -> dict[str, Any]:
    """Advertise exactly the list surfaces implemented by the BHM broker."""

    return {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
        "prompts": {"listChanged": False},
    }


def protocol_conformance_matrix() -> list[dict[str, Any]]:
    """Return the bounded matrix used by deterministic and live validators."""

    return [
        {
            "id": "initialize_supported",
            "method": "initialize",
            "kind": "request",
            "expected": "result",
            "capability": "lifecycle",
        },
        {
            "id": "initialize_unsupported_version",
            "method": "initialize",
            "kind": "request",
            "expected": "error:-32602",
            "capability": "lifecycle",
        },
        {
            "id": "initialized_notification",
            "method": "notifications/initialized",
            "kind": "notification",
            "expected": "no-response",
            "capability": "lifecycle",
        },
        {
            "id": "ping",
            "method": "ping",
            "kind": "request",
            "expected": "result",
            "capability": "lifecycle",
        },
        {
            "id": "tools_list",
            "method": "tools/list",
            "kind": "request",
            "expected": "result.tools",
            "capability": "tools",
        },
        {
            "id": "resources_list",
            "method": "resources/list",
            "kind": "request",
            "expected": "result.resources",
            "capability": "resources",
        },
        {
            "id": "resource_templates_list",
            "method": "resources/templates/list",
            "kind": "request",
            "expected": "result.resourceTemplates",
            "capability": "resources",
        },
        {
            "id": "prompts_list",
            "method": "prompts/list",
            "kind": "request",
            "expected": "result.prompts",
            "capability": "prompts",
        },
        {
            "id": "tool_call",
            "method": "tools/call",
            "kind": "request",
            "expected": "result.content",
            "capability": "tools",
        },
        {
            "id": "structured_unknown_method_error",
            "method": "bhm/unsupported",
            "kind": "request",
            "expected": "error:-32601",
            "capability": "unsupported",
        },
        {
            "id": "cancel_notification",
            "method": "notifications/cancelled",
            "kind": "notification",
            "expected": "no-response",
            "capability": "cancellation",
        },
        {
            "id": "cancel_request_fail_closed",
            "method": "cancel",
            "kind": "request",
            "expected": "error:-32601",
            "capability": "unsupported",
        },
        {
            "id": "shutdown",
            "method": "shutdown",
            "kind": "request",
            "expected": "result",
            "capability": "graceful-close",
        },
        {
            "id": "exit_notification",
            "method": "exit",
            "kind": "notification",
            "expected": "no-response",
            "capability": "graceful-close",
        },
        {
            "id": "transport_eof",
            "method": "transport/eof",
            "kind": "out-of-band",
            "expected": "exit-0",
            "capability": "graceful-close",
        },
    ]


def classify_response(
    method: str,
    response: Mapping[str, Any] | None,
    *,
    notification: bool = False,
) -> str:
    """Classify a response into the matrix vocabulary without inspecting raw data."""

    if notification:
        return "no-response" if response is None else "unexpected-response"
    if not isinstance(response, Mapping):
        return "missing-response"
    error = response.get("error")
    if isinstance(error, Mapping):
        return f"error:{int(error.get('code') or 0)}"
    result = response.get("result")
    if method == "tools/list":
        return "result.tools" if isinstance(result, Mapping) and isinstance(result.get("tools"), list) else "invalid-result"
    if method == "resources/list":
        return "result.resources" if isinstance(result, Mapping) and isinstance(result.get("resources"), list) else "invalid-result"
    if method == "resources/templates/list":
        return "result.resourceTemplates" if isinstance(result, Mapping) and isinstance(result.get("resourceTemplates"), list) else "invalid-result"
    if method == "prompts/list":
        return "result.prompts" if isinstance(result, Mapping) and isinstance(result.get("prompts"), list) else "invalid-result"
    if method == "tools/call":
        return "result.content" if isinstance(result, Mapping) and isinstance(result.get("content"), list) else "invalid-result"
    return "result"


def contract_snapshot() -> dict[str, Any]:
    """Return the bounded, JSON-safe protocol contract snapshot."""

    return {
        "schema_version": PROTOCOL_CONTRACT_SCHEMA_VERSION,
        "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": initialize_capabilities(),
        "matrix": protocol_conformance_matrix(),
        "error_codes": {
            "invalid_request": JSONRPC_INVALID_REQUEST,
            "method_not_found": JSONRPC_METHOD_NOT_FOUND,
            "invalid_params": JSONRPC_INVALID_PARAMS,
        },
        "error_taxonomy": error_contract_snapshot(),
    }
