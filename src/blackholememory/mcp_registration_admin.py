"""Admin MCP publication group: registered tools outside core/domain."""

from __future__ import annotations

from typing import Any


GROUP_NAME = "admin"


def select(tool_names: list[str] | tuple[str, ...] | set[str], *, core: set[str], domain: set[str]) -> list[str]:
    return sorted({str(name).strip() for name in tool_names if str(name).strip() not in core and str(name).strip() not in domain})


def select_tools(tools: list[Any], *, core: set[str], domain: set[str]) -> list[Any]:
    return [tool for tool in tools if str(getattr(tool, "name", tool) or "").strip() not in core | domain]
