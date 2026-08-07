from __future__ import annotations

from blackholememory.rest_mcp_parity import SCHEMA_VERSION
from blackholememory.rest_mcp_parity import build_rest_mcp_parity_inventory


def test_rest_mcp_parity_inventory_is_deterministic_and_route_complete() -> None:
    first = build_rest_mcp_parity_inventory()
    second = build_rest_mcp_parity_inventory()

    assert first == second
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["ok"] is True
    assert first["missing_routes"] == []
    assert first["route_wrappers"] >= 180
    assert first["direct_route_references"] >= 160
    assert first["mismatch_count"] == 0
    assert first["status"] == "aligned"
    assert first["compatibility_aliases"]
    assert first["compatibility_defaults"]
    assert any(
        item["mcp_field"] == "refs_csv" and item["rest_field"] == "refs"
        for item in first["compatibility_aliases"]
    )
    assert any(
        item["mcp_field"] == "items_json" and item["rest_field"] == "items"
        for item in first["compatibility_aliases"]
    )


def test_core_retrieval_and_forget_bounds_match_rest_contracts() -> None:
    report = build_rest_mcp_parity_inventory()
    core_group = {
        "bhm_search",
        "bhm_context_compile",
        "bhm_explain_retrieval",
        "bhm_forget_preview",
        "bhm_forget_apply",
    }

    assert not [item for item in report["mismatches"] if item["tool"] in core_group]
