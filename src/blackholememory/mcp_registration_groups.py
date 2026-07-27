"""Disjoint MCP registration groups used by the publication boundary."""

from __future__ import annotations

from typing import Any

from . import mcp_registration_admin
from . import mcp_registration_core
from . import mcp_registration_domain


GROUP_NAMES = (mcp_registration_core.GROUP_NAME, mcp_registration_domain.GROUP_NAME, mcp_registration_admin.GROUP_NAME)


def partition_registration_groups(tool_names: list[str] | tuple[str, ...] | set[str]) -> dict[str, list[str]]:
    normalized = sorted({str(name).strip() for name in tool_names if str(name).strip()})
    core = set(mcp_registration_core.select(normalized))
    domain = set(mcp_registration_domain.select(normalized))
    admin = set(mcp_registration_admin.select(normalized, core=core, domain=domain))
    return {
        mcp_registration_core.GROUP_NAME: sorted(core),
        mcp_registration_domain.GROUP_NAME: sorted(domain),
        mcp_registration_admin.GROUP_NAME: sorted(admin),
    }


def registration_group_report(tool_names: list[str] | tuple[str, ...] | set[str]) -> dict[str, Any]:
    groups = partition_registration_groups(tool_names)
    flattened = [name for group in GROUP_NAMES for name in groups[group]]
    return {
        "groups": groups,
        "counts": {group: len(groups[group]) for group in GROUP_NAMES},
        "complete": len(flattened) == len(set(flattened)) == len({str(name).strip() for name in tool_names if str(name).strip()}),
        "disjoint": len(flattened) == len(set(flattened)),
    }
