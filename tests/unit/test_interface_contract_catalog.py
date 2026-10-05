from __future__ import annotations

import pytest

from blackholememory.interface_contract_catalog import InterfaceCatalogError
from blackholememory.interface_contract_catalog import assert_legacy_core_compatible
from blackholememory.interface_contract_catalog import build_interface_contract_catalog
from blackholememory.interface_contract_catalog import discover_interface_contracts
from blackholememory.interface_contract_catalog import render_interface_contract_markdown
from blackholememory.interface_contract_catalog import verify_interface_contract_catalog


def _snapshots():
    openapi = {"paths": {"/bhm/health": {"get": {"responses": {"200": {"description": "ok"}}}}, "/bhm/memory": {"post": {"responses": {"201": {"description": "created"}}}}}}
    tools = [{"name": "bhm_health", "inputSchema": {"type": "object"}}, {"name": "bhm_remember", "inputSchema": {"type": "object", "required": ["content"]}}]
    metadata = {
        "GET:/bhm/health": {"scope": "core", "capability": "caller", "side_effect": "read-only"},
        "POST:/bhm/memory": {"scope": "operator", "capability": "caller-admin", "side_effect": "sqlite-authority"},
        "tool:bhm_health": {"scope": "core", "capability": "caller", "side_effect": "read-only"},
        "tool:bhm_remember": {"scope": "operator", "capability": "caller-admin", "side_effect": "operator-confirmed"},
    }
    return openapi, tools, metadata


def _catalog():
    openapi, tools, metadata = _snapshots()
    return build_interface_contract_catalog(openapi, tools, metadata=metadata, catalog_version="v1")


def test_catalog_is_deterministic_metadata_complete_and_renders_docs():
    first = _catalog()
    second = _catalog()

    assert first == second
    assert verify_interface_contract_catalog(first)
    assert first["execution"] == {"read_only": True, "runtime_mutation": False, "sqlite_mutation": False, "projection_mutation": False}
    document = render_interface_contract_markdown(first)
    assert "`tool:bhm_health`" in document
    assert "`POST:/bhm/memory`" in document
    assert "description\": \"created" not in document


def test_unknown_metadata_tampering_and_legacy_core_removal_fail_closed():
    openapi, tools, metadata = _snapshots()
    with pytest.raises(InterfaceCatalogError, match="missing explicit metadata"):
        build_interface_contract_catalog(openapi, tools, metadata={key: value for key, value in metadata.items() if key != "tool:bhm_health"}, catalog_version="v1")
    catalog = _catalog()
    assert not verify_interface_contract_catalog({**catalog, "entries": []})
    assert_legacy_core_compatible(catalog, ["GET:/bhm/health", "tool:bhm_health"])
    with pytest.raises(InterfaceCatalogError, match="missing"):
        assert_legacy_core_compatible(catalog, ["tool:bhm_missing"])


def test_progressive_discovery_hides_operator_contracts_without_capability():
    catalog = _catalog()
    core = discover_interface_contracts(catalog)
    assert [entry["operation"] for entry in core["entries"]] == ["tool:bhm_health", "GET:/bhm/health"]
    assert core["execution"] == {"read_only": True, "discovery_only": True, "runtime_mutation": False}
    with pytest.raises(InterfaceCatalogError, match="verified capability"):
        discover_interface_contracts(catalog, profile="operator")
    operator = discover_interface_contracts(catalog, profile="operator", operator_capability_verified=True)
    assert len(operator["entries"]) == 4
