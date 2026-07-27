from __future__ import annotations

import pytest

from blackholememory.mcp_catalog_contract import CatalogContractError
from blackholememory.mcp_catalog_contract import build_catalog_contract


def _initialize() -> dict:
    return {
        "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "bhm", "version": "ipc-broker-v1.7.1", "surface": "core"},
        }
    }


def test_catalog_contract_is_order_independent_and_hashes_full_schema():
    first = build_catalog_contract(
        _initialize(),
        {"result": {"tools": [{"name": "b", "description": "B"}, {"name": "a", "description": "A"}]}},
    )
    second = build_catalog_contract(
        _initialize(),
        {"result": {"tools": [{"name": "a", "description": "A"}, {"name": "b", "description": "B"}]}},
    )
    changed = build_catalog_contract(
        _initialize(),
        {"result": {"tools": [{"name": "a", "description": "changed"}, {"name": "b", "description": "B"}]}},
    )

    assert first.schema_hash == second.schema_hash
    assert first.generation == second.generation
    assert first.schema_hash != changed.schema_hash
    assert first.generation != changed.generation
    assert first.usable is True


def test_catalog_contract_carries_runtime_plugin_and_attach_generation():
    contract = build_catalog_contract(
        _initialize(),
        {"result": {"tools": [{"name": "a", "inputSchema": {"type": "object"}}]}},
        runtime_version="bhm-v9.0.0-TEST",
        plugin_version="9.0.0",
    )
    payload = contract.as_dict()

    assert payload["runtime_version"] == "bhm-v9.0.0-TEST"
    assert payload["plugin_version"] == "9.0.0"
    assert payload["attach_generation"] == payload["generation"]
    assert payload["server"] == {
        "id": "bhm",
        "version": "ipc-broker-v1.7.1",
        "surface": "core",
    }


def test_startup_complete_without_usable_catalog_is_unhealthy():
    contract = build_catalog_contract(_initialize(), {"result": {"tools": []}}, startup_complete=True)

    assert contract.startup_complete is True
    assert contract.usable is False
    assert contract.reason == "catalog_empty_or_malformed"


def test_incomplete_startup_is_unhealthy_even_with_tools():
    contract = build_catalog_contract(
        _initialize(),
        {"result": {"tools": [{"name": "a"}]}},
        startup_complete=False,
    )

    assert contract.usable is False
    assert contract.reason == "startup_incomplete"


def test_duplicate_tool_names_are_not_usable():
    contract = build_catalog_contract(
        _initialize(),
        {"result": {"tools": [{"name": "a"}, {"name": "a"}]}},
    )

    assert contract.usable is False
    assert contract.reason == "catalog_duplicate_tool_names"


def test_catalog_contract_rejects_unbounded_schema():
    with pytest.raises(CatalogContractError):
        build_catalog_contract(
            _initialize(),
            {"result": {"tools": [{"name": "a", "description": "x" * 1_100_000}]}},
        )
