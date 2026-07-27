from __future__ import annotations

from blackholememory.capability import admin_route_requires_capability
from blackholememory.capability import configured_admin_capability
from blackholememory.capability import extract_mcp_capability
from blackholememory.capability import is_admin_capability_valid


def test_capability_resolution_and_constant_time_validation(monkeypatch):
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "primary-secret")
    monkeypatch.setenv("BHM_MCP_ADMIN_CAPABILITY", "fallback-secret")

    assert configured_admin_capability() == "primary-secret"
    assert is_admin_capability_valid("primary-secret")
    assert not is_admin_capability_valid("fallback-secret")
    assert not is_admin_capability_valid("")


def test_mcp_capability_is_read_from_metadata_not_tool_arguments():
    assert extract_mcp_capability({"_meta": {"bhm_admin_capability": "meta-secret"}}) == "meta-secret"
    assert extract_mcp_capability({"capability": "direct-secret"}) == "direct-secret"
    assert extract_mcp_capability({"arguments": {"capability": "must-not-escape"}}) == ""


def test_admin_route_matching_is_explicit_and_boundary_safe():
    assert admin_route_requires_capability("/bhm/memory/hard", "POST")
    assert admin_route_requires_capability("/bhm/memory/link", "DELETE")
    assert admin_route_requires_capability("/bhm/memory/merge", "POST")
    assert admin_route_requires_capability("/bhm/forget/apply", "POST")
    assert admin_route_requires_capability("/bhm/admin/export", "POST")
    assert admin_route_requires_capability("/openapi-admin.json", "GET")
    assert admin_route_requires_capability("/bhm/memory", "DELETE")
    assert admin_route_requires_capability("/bhm/infra/purge-zombies", "POST")
    assert not admin_route_requires_capability("/bhm/infra/purge-zombies", "GET")
    assert not admin_route_requires_capability("/bhm/memory", "GET")
    assert not admin_route_requires_capability("/bhm/administer", "POST")
    assert not admin_route_requires_capability("/bhm/search", "POST")
