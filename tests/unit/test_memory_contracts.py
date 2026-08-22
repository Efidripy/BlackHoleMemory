"""REST/MCP memory metadata contract parity tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from blackholememory import app
from blackholememory import bhm_mcp
from blackholememory.memory_contracts import MemoryMetadata
from blackholememory.memory_contracts import MetadataDomain
from blackholememory.openapi_contract import build_openapi_schema


def _resolve_refs(value: Any, components: dict[str, Any]) -> Any:
    """Resolve local OpenAPI refs so REST and MCP shapes can be compared."""

    if isinstance(value, dict) and "$ref" in value:
        ref = str(value["$ref"])
        prefix = "#/components/schemas/"
        assert ref.startswith(prefix), f"unexpected OpenAPI reference: {ref}"
        return _resolve_refs(components[ref.removeprefix(prefix)], components)
    if isinstance(value, dict):
        return {key: _resolve_refs(item, components) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(item, components) for item in value]
    return value


def _canonical_schema(value: Any) -> Any:
    """Drop generator-only titles while retaining contract semantics."""

    if isinstance(value, dict):
        return {
            key: _canonical_schema(item)
            for key, item in sorted(value.items())
            if key not in {"title"} and key != "default"
        }
    if isinstance(value, list):
        return [_canonical_schema(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _resolve_mcp_refs(value: Any, defs: dict[str, Any]) -> Any:
    """Resolve local MCP JSON Schema ``$defs`` without weakening semantics."""

    if isinstance(value, dict) and "$ref" in value:
        ref = str(value["$ref"])
        prefix = "#/$defs/"
        assert ref.startswith(prefix), f"unexpected MCP schema reference: {ref}"
        return _resolve_mcp_refs(defs[ref.removeprefix(prefix)], defs)
    if isinstance(value, dict):
        return {key: _resolve_mcp_refs(item, defs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_mcp_refs(item, defs) for item in value]
    return value


def _rest_request_model(schema: dict[str, Any], path: str) -> dict[str, Any]:
    operation = schema["paths"][path]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    components = schema["components"]["schemas"]
    return _resolve_refs(request_schema, components)


def _mcp_tool_map() -> dict[str, Any]:
    async def collect() -> list[Any]:
        return list(await bhm_mcp.mcp.list_tools())

    return {tool.name: tool for tool in asyncio.run(collect())}


def _mcp_request_model(tool: Any) -> dict[str, Any]:
    """Read the current MCP ``inputSchema`` with compatibility for old FastMCP."""

    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "parameters", None)
    assert isinstance(schema, dict), f"MCP tool schema missing for {getattr(tool, 'name', '<unknown>')}"
    defs = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}
    return _resolve_mcp_refs(schema, defs)


def _assert_metadata_schema(rest_model: dict[str, Any], rest_field: str, mcp_schema: dict[str, Any]) -> None:
    rest_metadata = rest_model["properties"][rest_field]
    rest_metadata = next(item for item in rest_metadata["anyOf"] if item.get("type") == "object")
    mcp_metadata = next(item for item in mcp_schema["anyOf"] if item.get("type") == "object")
    assert _canonical_schema(rest_metadata) == _canonical_schema(mcp_metadata)


def test_rest_and_mcp_import_the_same_memory_metadata_contract() -> None:
    assert app.MemoryMetadata is MemoryMetadata
    assert bhm_mcp.MemoryMetadata is MemoryMetadata
    assert app.MetadataDomain is MetadataDomain
    assert bhm_mcp.MetadataDomain is MetadataDomain


def test_shared_metadata_contract_exposes_importance_score_and_bounds() -> None:
    metadata = MemoryMetadata(domain="backend", importance_score=7)

    assert metadata.model_dump(mode="json", exclude_none=True) == {
        "domain": "backend",
        "importance_score": 7,
    }
    assert "importance_score" in MemoryMetadata.model_json_schema()["properties"]

    with pytest.raises(ValueError):
        MemoryMetadata(importance_score=0)


def test_rest_and_mcp_generated_memory_metadata_schemas_are_identical() -> None:
    """Fail closed if a transport changes enum/default/bounds/description semantics."""

    rest_schema = build_openapi_schema(app.app, "public")
    tools = _mcp_tool_map()

    remember_model = _rest_request_model(rest_schema, "/bhm/remember")
    remember_tool = _mcp_request_model(tools["bhm_remember"])
    _assert_metadata_schema(remember_model, "metadata", remember_tool["properties"]["metadata"])

    upsert_model = _rest_request_model(rest_schema, "/bhm/memory/upsert")
    batch_upsert_model = _mcp_request_model(tools["bhm_batch_upsert"])
    batch_upsert_item = batch_upsert_model["properties"]["items"]["items"]
    _assert_metadata_schema(upsert_model, "metadata", batch_upsert_item["properties"]["metadata"])

    link_model = _rest_request_model(build_openapi_schema(app.app, "admin"), "/bhm/memory/link")
    batch_link_model = _mcp_request_model(tools["bhm_batch_link"])
    batch_link_item = batch_link_model["properties"]["items"]["items"]
    _assert_metadata_schema(link_model, "metadata", batch_link_item["properties"]["metadata"])


def test_mcp_json_metadata_adapter_uses_the_shared_contract() -> None:
    """String-encoded MCP adapters must retain the same validation bounds."""

    assert bhm_mcp._metadata_json_object('{"importance_score": 10}') == {"importance_score": 10}
    with pytest.raises(ValueError):
        bhm_mcp._metadata_json_object('{"importance_score": 11}')
