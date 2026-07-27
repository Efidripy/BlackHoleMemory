from __future__ import annotations

import ast
from pathlib import Path

from blackholememory.mcp_registration_groups import registration_group_report
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.mcp_surfaces import EXTENDED_PUBLIC_TOOL_NAMES


REPO_ROOT = Path(__file__).resolve().parents[2]


def registered_tool_names() -> list[str]:
    source = REPO_ROOT / "src" / "blackholememory" / "bhm_mcp.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "tool":
            continue
        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                names.append(keyword.value.value)
    return names


def test_registration_groups_are_complete_disjoint_and_fail_closed():
    names = registered_tool_names()
    report = registration_group_report(names)
    groups = report["groups"]
    assert report["complete"] is True
    assert report["disjoint"] is True
    assert report["counts"]["core"] == len(CORE_TOOL_NAMES)
    assert report["counts"]["domain"] == len(EXTENDED_PUBLIC_TOOL_NAMES) == 84
    assert report["counts"]["admin"] == 67
    assert set(groups["core"]).isdisjoint(groups["domain"])
    assert set(groups["core"]).isdisjoint(groups["admin"])
    assert set(groups["domain"]).isdisjoint(groups["admin"])
