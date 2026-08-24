from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.mcp_surfaces import EXTENDED_PUBLIC_TOOL_NAMES
from blackholememory.mcp_surfaces import McpSurface
from blackholememory.mcp_surfaces import catalog_report
from blackholememory.mcp_surfaces import filter_tools
from blackholememory.mcp_surfaces import is_tool_allowed
from blackholememory.mcp_surfaces import resolve_mcp_surface


def _registered_tool_names() -> list[str]:
    source_path = Path(__file__).resolve().parents[2] / "src" / "blackholememory" / "bhm_mcp.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "tool":
            continue
        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                names.append(keyword.value.value)
    return names


def test_registered_catalog_has_no_missing_or_duplicate_core_tools():
    report = catalog_report(_registered_tool_names())

    assert report["registered_count"] == report["core_count"] + report["admin_count"]
    assert report["core_count"] == len(CORE_TOOL_NAMES)
    assert report["extended_public_count"] == len(EXTENDED_PUBLIC_TOOL_NAMES) == 84
    assert report["missing_core"] == []
    assert report["missing_extended_public"] == []
    assert report["duplicates"] == []
    assert report["admin_count"] == 159


def test_surface_resolution_fails_closed_and_supports_operator_aliases(monkeypatch):
    monkeypatch.delenv("BHM_MCP_SURFACE", raising=False)
    assert resolve_mcp_surface() is McpSurface.CORE
    assert resolve_mcp_surface("stable") is McpSurface.CORE
    assert resolve_mcp_surface("operator") is McpSurface.ADMIN
    assert resolve_mcp_surface("untrusted-value") is McpSurface.CORE


def test_filter_tools_hides_unapproved_tools_from_core_and_preserves_order():
    tools = [
        SimpleNamespace(name="bhm_health"),
        SimpleNamespace(name="bhm_admin_export"),
        SimpleNamespace(name="bhm_search"),
    ]

    core_tools = filter_tools(tools, McpSurface.CORE)
    assert [tool.name for tool in core_tools] == ["bhm_health", "bhm_search"]
    assert [tool.name for tool in filter_tools(tools, McpSurface.ADMIN)] == [
        "bhm_health",
        "bhm_admin_export",
        "bhm_search",
    ]


def test_core_surface_rejects_unlisted_tools_before_dispatch():
    assert is_tool_allowed("bhm_health", McpSurface.CORE)
    assert not is_tool_allowed("bhm_admin_export", McpSurface.CORE)
    assert is_tool_allowed("bhm_admin_export", McpSurface.ADMIN)
