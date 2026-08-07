"""Read-only REST/MCP schema parity inventory.

The inventory deliberately separates route reachability from semantic parity.
Compatibility wrappers may use CSV aliases or intentionally different defaults;
those differences remain visible as residuals instead of being silently
accepted as a complete P1-21 closure.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from . import app
from . import bhm_mcp
from .openapi_contract import build_openapi_schema


SCHEMA_VERSION = "bhm.rest-mcp.parity-inventory.v1"
_HTTP_HELPERS = {"_get", "_post", "_delete"}

# MCP keeps a stable, tool-friendly argument vocabulary while REST keeps the
# canonical JSON wire shape.  These are explicit compatibility mappings, not
# a blanket snake_case/camelCase rewrite: changing a public MCP argument name
# would break existing callers and hide the adapter boundary we are auditing.
_MCP_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "bhm_upsert_memory": {"memory_type": "type"},
    "bhm_task_close": {"next_step": "next"},
}
_GENERIC_MCP_FIELD_ALIASES = {
    "artifact_ids_csv": "artifact_ids",
    "concepts_csv": "concepts",
    "data_json": "data",
    "files_csv": "files",
    "files_touched_csv": "files_touched",
    "hook_type": "hookType",
    "ids_csv": "ids",
    "items_json": "items",
    "metadata_json": "metadata",
    "parent_event_id": "parentEventId",
    "queue_ids_csv": "queue_ids",
    "refs_csv": "refs",
    "schema_version": "schemaVersion",
    "scope_in_csv": "scope_in",
    "scope_out_csv": "scope_out",
    "session_id": "sessionId",
    "source_ids_csv": "source_ids",
    "timestamp": "timestamp",
}
_TYPE_COERCION_COMPATIBILITY_FIELDS = {
    ("bhm_observe", "timestamp"),  # MCP string -> REST datetime parser
}


def _mcp_field_alias(tool_name: str, field: str) -> str:
    return _MCP_FIELD_ALIASES.get(tool_name, {}).get(field, _GENERIC_MCP_FIELD_ALIASES.get(field, field))


def _is_compatibility_alias(tool_name: str, mcp_field: str, rest_field: str) -> bool:
    return mcp_field != rest_field or (
        mcp_field in {"items_json", "artifact_ids_csv", "ids_csv", "refs_csv", "source_ids_csv", "data_json"}
        and rest_field == _mcp_field_alias(tool_name, mcp_field)
    ) or (tool_name == "bhm_batch_upsert" and rest_field == "items")


def _schema_default(schema: Any) -> Any:
    return schema.get("default") if isinstance(schema, dict) and "default" in schema else None


def _normalize_mcp_schema(tool_name: str, value: Any) -> Any:
    """Normalize nested MCP model keys without changing the public catalog."""

    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "properties" and isinstance(item, dict):
                normalized[key] = {
                    _mcp_field_alias(tool_name, str(field)): _normalize_mcp_schema(tool_name, field_schema)
                    for field, field_schema in item.items()
                }
            elif key == "required" and isinstance(item, list):
                normalized[key] = [_mcp_field_alias(tool_name, str(field)) for field in item]
            else:
                normalized[key] = _normalize_mcp_schema(tool_name, item)
        return normalized
    if isinstance(value, list):
        return [_normalize_mcp_schema(tool_name, item) for item in value]
    return value


def _resolve_openapi(value: Any, components: dict[str, Any]) -> Any:
    if isinstance(value, dict) and "$ref" in value:
        ref = str(value["$ref"])
        prefix = "#/components/schemas/"
        if ref.startswith(prefix):
            return _resolve_openapi(components[ref.removeprefix(prefix)], components)
    if isinstance(value, dict):
        return {key: _resolve_openapi(item, components) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_openapi(item, components) for item in value]
    return value


def _resolve_mcp(value: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(value, dict) and "$ref" in value:
        ref = str(value["$ref"])
        prefix = "#/$defs/"
        if ref.startswith(prefix):
            return _resolve_mcp(definitions[ref.removeprefix(prefix)], definitions)
    if isinstance(value, dict):
        return {key: _resolve_mcp(item, definitions) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_mcp(item, definitions) for item in value]
    return value


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonical(item)
            for key, item in sorted(value.items())
            if key not in {"title", "$schema"} and not (key == "default" and item is None)
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _decorated_route_map() -> dict[str, list[tuple[str, str]]]:
    source_path = Path(__file__).with_name("bhm_mcp.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    result: dict[str, list[tuple[str, str]]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tool_name: str | None = None
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr != "tool":
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    tool_name = str(keyword.value.value)
        if not tool_name:
            continue
        paths = {
            (call.func.id, str(call.args[0].value))
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in _HTTP_HELPERS
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        }
        result[tool_name] = sorted(paths)
    return result


def _operation_schema(
    method: str,
    path: str,
    public_schema: dict[str, Any],
    admin_schema: dict[str, Any],
    components: dict[str, Any],
) -> dict[str, Any] | None:
    schema = admin_schema if path in admin_schema.get("paths", {}) else public_schema
    operation = schema.get("paths", {}).get(path, {}).get(method.removeprefix("_"))
    if not isinstance(operation, dict):
        return None
    # FastAPI exposes primitive parameters as query parameters even for POST
    # and DELETE operations.  The previous inventory only inspected the body
    # for POST, so routes such as ``adr/supersede`` and ``slot`` lost required
    # query fields and produced false drift.  Merge both transport locations;
    # duplicate names are intentionally resolved in favour of the request
    # body because that is the canonical JSON shape for typed request models.
    properties: dict[str, Any] = {}
    required: set[str] = set()
    for parameter in operation.get("parameters", []):
        if parameter.get("in") != "query" or "name" not in parameter:
            continue
        name = str(parameter["name"])
        properties[name] = parameter.get("schema", {})
        if parameter.get("required"):
            required.add(name)

    request_schema = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    resolved = _resolve_openapi(request_schema, components)
    if isinstance(resolved, dict):
        properties.update(resolved.get("properties", {}))
        required.update(resolved.get("required", []))

    if not properties and not required:
        return None
    return {
        "properties": properties,
        "required": required,
    }


def build_rest_mcp_parity_inventory() -> dict[str, Any]:
    public_schema = build_openapi_schema(app.app, "public")
    admin_schema = build_openapi_schema(app.app, "admin")
    components = {
        **public_schema.get("components", {}).get("schemas", {}),
        **admin_schema.get("components", {}).get("schemas", {}),
    }
    route_map = _decorated_route_map()
    tools = {tool.name: tool for tool in asyncio.run(bhm_mcp.mcp.list_tools())}
    all_paths = set(public_schema.get("paths", {})) | set(admin_schema.get("paths", {}))
    route_refs = [
        (name, method, path)
        for name, routes in route_map.items()
        for method, path in routes
    ]
    missing_routes = [
        {"tool": name, "method": method, "path": path}
        for name, method, path in route_refs
        if path not in all_paths
    ]
    mismatches: list[dict[str, Any]] = []
    compatibility_aliases: list[dict[str, Any]] = []
    compatibility_defaults: list[dict[str, Any]] = []
    comparable = 0
    for name, routes in sorted(route_map.items()):
        if len(routes) != 1 or name not in tools:
            continue
        method, path = routes[0]
        operation = _operation_schema(method, path, public_schema, admin_schema, components)
        if operation is None:
            continue
        schema = getattr(tools[name], "inputSchema", None) or getattr(tools[name], "parameters", None) or {}
        if not isinstance(schema, dict):
            continue
        comparable += 1
        mcp_properties = schema.get("properties", {})
        rest_properties = operation["properties"]
        mapped_required = {_mcp_field_alias(name, str(field)) for field in schema.get("required", [])}
        rest_required = set(operation["required"])

        # A compatibility wrapper may supply a REST-required project from its
        # stable MCP default.  Keep that fact visible, but do not call it a
        # required-field drift when the adapter always materializes a value.
        for rest_field in sorted(rest_required - mapped_required):
            mcp_field = next(
                (field for field in mcp_properties if _mcp_field_alias(name, str(field)) == rest_field),
                None,
            )
            if mcp_field is None:
                continue
            mcp_schema = _normalize_mcp_schema(
                name,
                _resolve_mcp(mcp_properties[mcp_field], schema.get("$defs", {})),
            )
            if _schema_default(mcp_schema) is not None:
                compatibility_defaults.append(
                    {
                        "tool": name,
                        "path": path,
                        "mcp_field": mcp_field,
                        "rest_field": rest_field,
                        "default": _schema_default(mcp_schema),
                    }
                )
                mapped_required.add(rest_field)

        if rest_required != mapped_required:
            mismatches.append(
                {
                    "tool": name,
                    "path": path,
                    "kind": "required",
                    "rest": sorted(rest_required),
                    "mcp": sorted(mapped_required),
                }
            )
        for mcp_field in sorted(mcp_properties):
            field = _mcp_field_alias(name, str(mcp_field))
            if field not in rest_properties:
                continue
            rest_value = _resolve_openapi(rest_properties[field], components)
            mcp_value = _normalize_mcp_schema(
                name,
                _resolve_mcp(mcp_properties[mcp_field], schema.get("$defs", {})),
            )
            if _canonical(rest_value) != _canonical(mcp_value):
                if (
                    mcp_field == "project"
                    and _schema_default(mcp_value) is not None
                    and isinstance(rest_value, dict)
                    and (
                        any(
                            isinstance(option, dict) and option.get("type") == "null"
                            for option in rest_value.get("anyOf", [])
                        )
                        or field in operation["required"]
                    )
                ):
                    compatibility_defaults.append(
                        {
                            "tool": name,
                            "path": path,
                            "mcp_field": mcp_field,
                            "rest_field": field,
                            "default": _schema_default(mcp_value),
                            "reason": "REST nullable project accepts MCP adapter default",
                        }
                    )
                    continue
                if (name, str(mcp_field)) in _TYPE_COERCION_COMPATIBILITY_FIELDS:
                    compatibility_aliases.append(
                        {
                            "tool": name,
                            "path": path,
                            "mcp_field": mcp_field,
                            "rest_field": field,
                            "mcp_sha256": _digest(mcp_value),
                            "rest_sha256": _digest(rest_value),
                            "reason": "bounded REST type coercion",
                        }
                    )
                    continue
                if _is_compatibility_alias(name, str(mcp_field), field):
                    compatibility_aliases.append(
                        {
                            "tool": name,
                            "path": path,
                            "mcp_field": mcp_field,
                            "rest_field": field,
                            "mcp_sha256": _digest(mcp_value),
                            "rest_sha256": _digest(rest_value),
                        }
                    )
                    continue
                mismatches.append(
                    {
                        "tool": name,
                        "path": path,
                        "field": mcp_field,
                        "kind": "schema",
                        "rest_sha256": _digest(rest_value),
                        "mcp_sha256": _digest(mcp_value),
                    }
                )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "residual" if mismatches else "aligned",
        "ok": not missing_routes,
        "route_wrappers": len(route_map),
        "direct_route_references": len(route_refs),
        "comparable_single_route_tools": comparable,
        "missing_routes": missing_routes,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:128],
        "mismatches_truncated": len(mismatches) > 128,
        "compatibility_aliases": compatibility_aliases[:128],
        "compatibility_aliases_truncated": len(compatibility_aliases) > 128,
        "compatibility_defaults": compatibility_defaults[:128],
        "compatibility_defaults_truncated": len(compatibility_defaults) > 128,
    }


__all__ = ["SCHEMA_VERSION", "build_rest_mcp_parity_inventory"]
