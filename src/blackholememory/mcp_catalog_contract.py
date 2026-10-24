"""Deterministic MCP catalog identity and attach-generation contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .version_manifest import BROKER_VERSION
from .version_manifest import PLUGIN_VERSION
from .version_manifest import RUNTIME_VERSION


SCHEMA_VERSION = "bhm.mcp.catalog-contract.v1"
MAX_CANONICAL_BYTES = 1_048_576


class CatalogContractError(ValueError):
    """Raised when an MCP initialize/catalog pair cannot form a contract."""


def _sha256(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalogContractError("catalog contract contains non-serializable data") from exc
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise CatalogContractError("catalog contract exceeds bounded canonical size")
    return hashlib.sha256(encoded).hexdigest()


def _tool_schema(item: Mapping[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    description = str(item.get("description") or "")
    input_schema = item.get("inputSchema")
    if input_schema is None:
        input_schema = item.get("input_schema")
    if not isinstance(input_schema, Mapping):
        input_schema = {}
    return {
        "name": name,
        "description": description,
        "inputSchema": dict(input_schema),
    }


def _canonical_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    tools = [schema for item in value if isinstance(item, Mapping) if (schema := _tool_schema(item)) is not None]
    return sorted(tools, key=lambda item: (item["name"], json.dumps(item, ensure_ascii=False, sort_keys=True)))


@dataclass(frozen=True)
class CatalogContract:
    schema_hash: str
    generation: str
    attach_generation: str
    server_id: str
    server_version: str
    runtime_version: str
    plugin_version: str
    surface: str
    protocol_version: str
    tool_count: int
    startup_complete: bool
    usable: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "schema_hash": self.schema_hash,
            "generation": self.generation,
            "attach_generation": self.attach_generation,
            "server": {
                "id": self.server_id,
                "version": self.server_version,
                "surface": self.surface,
            },
            "runtime_version": self.runtime_version,
            "plugin_version": self.plugin_version,
            "protocol_version": self.protocol_version,
            "tool_count": self.tool_count,
            "startup_complete": self.startup_complete,
            "usable": self.usable,
            "reason": self.reason,
        }


def build_catalog_contract(
    initialize_response: Mapping[str, Any],
    catalog_response: Mapping[str, Any],
    *,
    startup_complete: bool = True,
    runtime_version: str = RUNTIME_VERSION,
    plugin_version: str = PLUGIN_VERSION,
) -> CatalogContract:
    """Build stable schema and generation digests from MCP protocol responses."""

    initialize_result = initialize_response.get("result")
    catalog_result = catalog_response.get("result")
    server_info = initialize_result.get("serverInfo") if isinstance(initialize_result, Mapping) else {}
    if not isinstance(server_info, Mapping):
        server_info = {}
    tools = catalog_result.get("tools") if isinstance(catalog_result, Mapping) else []
    canonical_tools = _canonical_tools(tools)
    raw_tools = tools if isinstance(tools, list) else []
    protocol_version = str((initialize_result or {}).get("protocolVersion") or "") if isinstance(initialize_result, Mapping) else ""
    server_id = str(server_info.get("name") or "").strip()
    server_version = str(server_info.get("version") or BROKER_VERSION).strip()
    surface = str(server_info.get("surface") or "core").strip()
    runtime = str(runtime_version or RUNTIME_VERSION).strip()
    plugin = str(plugin_version or PLUGIN_VERSION).strip()
    schema_hash = _sha256({"protocol_version": protocol_version, "tools": canonical_tools})
    generation = _sha256(
        {
            "server": {"id": server_id, "version": server_version, "surface": surface},
            "runtime_version": runtime,
            "plugin_version": plugin,
            "schema_hash": schema_hash,
        }
    )
    names = [str(item["name"]) for item in canonical_tools]
    duplicate_names = len(names) != len(set(names))
    malformed_entries = not isinstance(tools, list) or len(canonical_tools) != len(raw_tools)
    if not startup_complete:
        reason = "startup_incomplete"
    elif not server_id:
        reason = "server_identity_missing"
    elif not protocol_version:
        reason = "protocol_version_missing"
    elif not canonical_tools or malformed_entries:
        reason = "catalog_empty_or_malformed"
    elif duplicate_names:
        reason = "catalog_duplicate_tool_names"
    else:
        reason = "usable"
    usable = reason == "usable"
    return CatalogContract(
        schema_hash=schema_hash,
        generation=generation,
        attach_generation=generation,
        server_id=server_id,
        server_version=server_version,
        runtime_version=runtime,
        plugin_version=plugin,
        surface=surface,
        protocol_version=protocol_version,
        tool_count=len(canonical_tools),
        startup_complete=bool(startup_complete),
        usable=usable,
        reason=reason,
    )


def catalog_generation(initialize_response: Mapping[str, Any], catalog_response: Mapping[str, Any]) -> str:
    """Compatibility helper returning the deterministic attach generation."""

    return build_catalog_contract(initialize_response, catalog_response).generation


__all__ = [
    "CatalogContract",
    "CatalogContractError",
    "SCHEMA_VERSION",
    "build_catalog_contract",
    "catalog_generation",
]
