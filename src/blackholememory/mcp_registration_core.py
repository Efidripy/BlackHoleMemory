"""Core MCP publication group: the bounded attach catalog."""

from __future__ import annotations

from typing import Any

from .mcp_surfaces import CORE_TOOL_NAMES


GROUP_NAME = "core"


def contains(tool_name: str) -> bool:
    return str(tool_name).strip() in CORE_TOOL_NAMES


def select(tool_names: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    return sorted({str(name).strip() for name in tool_names if contains(str(name))})


def select_tools(tools: list[Any]) -> list[Any]:
    return [tool for tool in tools if contains(str(getattr(tool, "name", tool) or ""))]
