"""Transport contract, constants, and hashing helpers for BHM Streamable HTTP."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

JsonRpcDispatcher = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

SCHEMA_VERSION = "bhm.mcp.streamable-http.v1"
TRANSPORT_CONTRACT_SCHEMA_VERSION = "bhm.mcp.transport-contract.v1"
DEFAULT_RETRY_INTERVAL_MS = 1_000
DEFAULT_SESSION_IDLE_SECONDS = 300.0
DEFAULT_MAX_SESSIONS = 32
DEFAULT_MAX_BODY_BYTES = 1_048_576
DEFAULT_DISPATCH_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CONCURRENT_CALLS = 10


class StreamableHttpContractError(RuntimeError):
    """Raised when the shared BHM JSON-RPC surface returns an invalid result."""


def _result(response: dict[str, Any] | None, *, method: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise StreamableHttpContractError(f"{method} returned no JSON-RPC response")
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code", "error")
        message = str(error.get("message") or "MCP request failed")
        raise StreamableHttpContractError(f"{method} failed ({code}): {message}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise StreamableHttpContractError(f"{method} returned no result object")
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _catalog_hash(tools: Any) -> str | None:
    if not isinstance(tools, list):
        return None
    payload = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _transport_contract_digest(
    catalog_hash: str | None,
    *,
    server_id: str,
    server_version: str,
    protocol_version: str,
) -> str | None:
    """Build a deterministic, metadata-only identity for one transport catalog."""

    if not catalog_hash:
        return None
    payload = {
        "schema_version": TRANSPORT_CONTRACT_SCHEMA_VERSION,
        "server_id": str(server_id or "bhm")[:64],
        "server_version": str(server_version or "unknown")[:64],
        "protocol_version": str(protocol_version or "unknown")[:32],
        "catalog_hash": str(catalog_hash)[:128],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_DISPATCH_TIMEOUT_SECONDS",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_CONCURRENT_CALLS",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_RETRY_INTERVAL_MS",
    "DEFAULT_SESSION_IDLE_SECONDS",
    "JsonRpcDispatcher",
    "SCHEMA_VERSION",
    "StreamableHttpContractError",
    "TRANSPORT_CONTRACT_SCHEMA_VERSION",
    "_catalog_hash",
    "_result",
    "_transport_contract_digest",
    "_utc_now",
]
