"""Admin MCP publication group: registered tools outside core/domain."""

from __future__ import annotations



GROUP_NAME = "admin"


def select(tool_names: list[str] | tuple[str, ...] | set[str], *, core: set[str], domain: set[str]) -> list[str]:
    return sorted({str(name).strip() for name in tool_names if str(name).strip() not in core and str(name).strip() not in domain})
