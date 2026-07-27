"""Domain MCP publication group: reviewed non-core public tools."""

from __future__ import annotations

from typing import Any

from .mcp_surfaces import EXTENDED_PUBLIC_TOOL_NAMES


GROUP_NAME = "domain"


def contains(tool_name: str) -> bool:
    return str(tool_name).strip() in EXTENDED_PUBLIC_TOOL_NAMES


def select(tool_names: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    return sorted({str(name).strip() for name in tool_names if contains(str(name))})


def select_tools(tools: list[Any]) -> list[Any]:
    return [tool for tool in tools if contains(str(getattr(tool, "name", tool) or ""))]
