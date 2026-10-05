"""Offline, versioned REST/MCP contract catalogs with progressive discovery.

This module deliberately consumes snapshots supplied by a caller. It neither
registers routes/tools nor imports the runtime app, so the catalog is evidence
for compatibility review rather than a second authority or activation surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "bhm.interface-contract-catalog.v1"
MAX_ENTRIES = 512
_SIDE_EFFECTS = {"read-only", "sqlite-authority", "projection-only", "operator-confirmed"}
_SCOPES = {"core", "operator"}
_CAPABILITIES = {"caller", "caller-admin"}
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/{}-]{0,159}$")


class InterfaceCatalogError(ValueError):
    """Raised when a catalog cannot make a complete, bounded claim."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _name(value: Any, field_name: str) -> str:
    result = str(value or "").strip()
    if not _NAME.fullmatch(result):
        raise InterfaceCatalogError(f"{field_name} is invalid")
    return result


def _metadata(value: Any, operation: str) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        raise InterfaceCatalogError(f"{operation} is missing explicit metadata")
    scope = str(value.get("scope") or "")
    capability = str(value.get("capability") or "")
    side_effect = str(value.get("side_effect") or "")
    if scope not in _SCOPES or capability not in _CAPABILITIES or side_effect not in _SIDE_EFFECTS:
        raise InterfaceCatalogError(f"{operation} has unsafe metadata")
    if scope == "operator" and capability != "caller-admin":
        raise InterfaceCatalogError(f"{operation} operator scope requires caller-admin")
    if scope == "core" and capability != "caller":
        raise InterfaceCatalogError(f"{operation} core scope requires caller")
    return scope, capability, side_effect


def _schema_digest(value: Any) -> str:
    if not isinstance(value, Mapping):
        value = {}
    return _digest(dict(value))


def _rest_entries(openapi: Mapping[str, Any], metadata: Mapping[str, Any]) -> list[dict[str, str]]:
    paths = openapi.get("paths")
    if not isinstance(paths, Mapping):
        raise InterfaceCatalogError("OpenAPI paths are invalid")
    entries: list[dict[str, str]] = []
    for raw_path, item in paths.items():
        path = str(raw_path)
        if not path.startswith("/") or "?" in path or len(path) > 160:
            raise InterfaceCatalogError("OpenAPI path is unsafe")
        if not isinstance(item, Mapping):
            raise InterfaceCatalogError("OpenAPI path item is invalid")
        for raw_method, operation_data in item.items():
            method = str(raw_method).upper()
            if method not in _METHODS:
                continue
            if not isinstance(operation_data, Mapping):
                raise InterfaceCatalogError("OpenAPI operation is invalid")
            operation = f"{method}:{path}"
            scope, capability, side_effect = _metadata(metadata.get(operation), operation)
            entries.append(
                {
                    "interface": "rest",
                    "operation": operation,
                    "scope": scope,
                    "capability": capability,
                    "side_effect": side_effect,
                    "schema_digest": _schema_digest(operation_data),
                }
            )
    return entries


def _mcp_entries(tools: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise InterfaceCatalogError("MCP tool is invalid")
        name = _name(tool.get("name"), "MCP tool name")
        operation = f"tool:{name}"
        scope, capability, side_effect = _metadata(metadata.get(operation), operation)
        input_schema = tool.get("inputSchema", tool.get("input_schema", {}))
        entries.append(
            {
                "interface": "mcp",
                "operation": operation,
                "scope": scope,
                "capability": capability,
                "side_effect": side_effect,
                "schema_digest": _schema_digest(input_schema),
            }
        )
    return entries


def build_interface_contract_catalog(
    openapi: Mapping[str, Any],
    mcp_tools: Sequence[Mapping[str, Any]],
    *,
    metadata: Mapping[str, Any],
    catalog_version: str,
) -> dict[str, Any]:
    """Build one metadata-complete, digest-bound catalog from immutable snapshots."""

    version = _name(catalog_version, "catalog_version")
    entries = _rest_entries(openapi, metadata) + _mcp_entries(mcp_tools, metadata)
    if not entries or len(entries) > MAX_ENTRIES:
        raise InterfaceCatalogError("catalog entry count is unsafe")
    entries.sort(key=lambda item: (item["interface"], item["operation"]))
    names = [(item["interface"], item["operation"]) for item in entries]
    if len(names) != len(set(names)):
        raise InterfaceCatalogError("catalog has duplicate operations")
    core = {"schema_version": SCHEMA_VERSION, "catalog_version": version, "entries": entries}
    return {**core, "catalog_digest": _digest(core), "execution": {"read_only": True, "runtime_mutation": False, "sqlite_mutation": False, "projection_mutation": False}}


def verify_interface_contract_catalog(catalog: Mapping[str, Any]) -> bool:
    core = {key: catalog.get(key) for key in ("schema_version", "catalog_version", "entries")}
    if core["schema_version"] != SCHEMA_VERSION or not isinstance(core["entries"], list):
        return False
    try:
        _name(core["catalog_version"], "catalog_version")
    except InterfaceCatalogError:
        return False
    entries = core["entries"]
    if not entries or len(entries) > MAX_ENTRIES or len({(item.get("interface"), item.get("operation")) for item in entries if isinstance(item, Mapping)}) != len(entries):
        return False
    for item in entries:
        if not isinstance(item, Mapping):
            return False
        if set(item) != {"interface", "operation", "scope", "capability", "side_effect", "schema_digest"}:
            return False
        try:
            _name(item.get("operation"), "catalog operation")
            _metadata({"scope": item.get("scope"), "capability": item.get("capability"), "side_effect": item.get("side_effect")}, str(item.get("operation")))
        except InterfaceCatalogError:
            return False
        if item.get("interface") not in {"rest", "mcp"} or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("schema_digest") or "")):
            return False
    return catalog.get("catalog_digest") == _digest(core)


def discover_interface_contracts(
    catalog: Mapping[str, Any],
    *,
    profile: str = "core",
    operator_capability_verified: bool = False,
) -> dict[str, Any]:
    """Reveal core by default; operator entries require explicit capability proof."""

    if not verify_interface_contract_catalog(catalog):
        raise InterfaceCatalogError("catalog digest is invalid")
    if profile not in {"core", "operator"}:
        raise InterfaceCatalogError("discovery profile is invalid")
    if profile == "operator" and not operator_capability_verified:
        raise InterfaceCatalogError("operator discovery requires verified capability")
    entries = [entry for entry in catalog["entries"] if entry["scope"] == "core" or profile == "operator"]
    core = {"schema_version": SCHEMA_VERSION, "catalog_version": catalog["catalog_version"], "profile": profile, "entries": entries}
    return {**core, "discovery_digest": _digest(core), "execution": {"read_only": True, "discovery_only": True, "runtime_mutation": False}}


def assert_legacy_core_compatible(catalog: Mapping[str, Any], legacy_core_operations: Sequence[str]) -> None:
    """Fail when a pre-existing core operation disappears or changes its scope."""

    if not verify_interface_contract_catalog(catalog):
        raise InterfaceCatalogError("catalog digest is invalid")
    expected = {_name(operation, "legacy core operation") for operation in legacy_core_operations}
    actual = {entry["operation"] for entry in catalog["entries"] if entry["scope"] == "core"}
    missing = sorted(expected - actual)
    if missing:
        raise InterfaceCatalogError(f"legacy core operations are missing: {', '.join(missing)}")


def render_interface_contract_markdown(catalog: Mapping[str, Any]) -> str:
    """Generate bounded, content-free contract documentation without writing it."""

    if not verify_interface_contract_catalog(catalog):
        raise InterfaceCatalogError("catalog digest is invalid")
    lines = [
        "# BHM interface contract catalog",
        "",
        f"- Schema: `{catalog['schema_version']}`",
        f"- Catalog version: `{catalog['catalog_version']}`",
        f"- Digest: `{catalog['catalog_digest']}`",
        "",
        "| Interface | Operation | Scope | Capability | Side effect | Schema digest |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in catalog["entries"]:
        lines.append("| {interface} | `{operation}` | {scope} | {capability} | {side_effect} | `{schema_digest}` |".format(**entry))
    return "\n".join(lines) + "\n"


__all__ = [
    "InterfaceCatalogError",
    "SCHEMA_VERSION",
    "assert_legacy_core_compatible",
    "build_interface_contract_catalog",
    "discover_interface_contracts",
    "render_interface_contract_markdown",
    "verify_interface_contract_catalog",
]
