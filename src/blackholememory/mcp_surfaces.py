"""Explicit MCP catalog boundaries for ordinary agents and operators.

The FastMCP registry intentionally remains one compatibility implementation
registry. Registration ownership is partitioned by
``mcp_registration_core/domain/admin`` before publication: the default
``core`` surface exposes a small attach catalog, while ``admin`` may expose the
complete registered catalog to an explicitly configured operator process.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Any


MCP_SURFACE_ENV = "BHM_MCP_SURFACE"


class McpSurface(StrEnum):
    """Published MCP catalog selected for the current API/broker process."""

    CORE = "core"
    ADMIN = "admin"


# This inventory is the reviewable stable compatibility list; only
# CORE_TOOL_NAMES is published by default.
GOVERNANCE_PUBLIC_TOOL_NAMES = frozenset(
    {
        "bhm_health",
        "bhm_health_slo",
        "bhm_diagnostics",
        "bhm_preflight",
        "bhm_profile",
        "bhm_insights",
        "bhm_search",
        "bhm_context_compile",
        "bhm_explain_retrieval",
        "bhm_memory_used",
        "bhm_get_memory",
        "bhm_list_memories",
        "bhm_update_memory",
        "bhm_archive_memory",
        "bhm_forget_preview",
        "bhm_search_advanced",
        "bhm_recent_activity",
        "bhm_upsert_memory",
        "bhm_delete_memory",
        "bhm_merge_memories",
        "bhm_detect_duplicates",
        "bhm_detect_conflicts",
        "bhm_memory_lint",
        "bhm_memory_diff",
        "bhm_memory_compact",
        "bhm_memory_changelog",
        "bhm_get_memory_links",
        "bhm_link_memories",
        "bhm_unlink_memories",
        "bhm_relation_suggest",
        "bhm_link_graph_stats",
        "bhm_crystallize",
        "bhm_checkpoint_create",
        "bhm_checkpoint_list",
        "bhm_checkpoint_get_latest",
        "bhm_project_map_get",
        "bhm_project_resolve",
        "bhm_project_map_upsert",
        "bhm_project_retire",
        "bhm_project_summary_get",
        "bhm_project_summary_pin",
        "bhm_project_summary_list",
        "bhm_rebuild_project_summary",
        "bhm_project_summary_compare",
        "bhm_task_context_get",
        "bhm_task_context_update",
        "bhm_risk_register_get",
        "bhm_risk_register_update",
        "bhm_validation_snapshot_get",
        "bhm_validation_snapshot_save",
        "bhm_validation_trend_report",
        "bhm_get_memories_by_concept",
        "bhm_get_memories_by_type",
        "bhm_set_memory_confidence",
        "bhm_vote_memory_quality",
        "bhm_pin_memory",
        "bhm_unpin_memory",
        "bhm_list_pinned_memories",
        "bhm_query_suggestions",
        "bhm_memory_timeline",
        "bhm_search_hybrid",
        "bhm_search_by_source_ref",
        "bhm_search_by_upsert_key",
        "bhm_adr_create",
        "bhm_adr_list",
        "bhm_adr_supersede",
        "bhm_handoff_create",
        "bhm_handoff_list",
        "bhm_session_record_create",
        "bhm_session_record_list",
        "bhm_task_open",
        "bhm_task_close",
        "bhm_task_get",
        "bhm_task_list",
        "bhm_source_refs_attach",
        "bhm_source_refs_get",
        "bhm_source_refs_detach",
        "bhm_source_refs_replace",
        "bhm_memory_alias_add",
        "bhm_memory_alias_remove",
        "bhm_alias_resolve",
        "bhm_alias_stats",
        "bhm_entity_extract",
        "bhm_entity_catalog_get",
        "bhm_entity_search",
        "bhm_remember",
        "bhm_lessons_create",
        "bhm_lessons_search",
        "bhm_observe",
        "bhm_slot_list",
        "bhm_slot_get",
        "bhm_slot_set",
        "bhm_slot_append",
        "bhm_slot_replace",
        "bhm_slot_delete",
        "bhm_slot_reflect",
        "bhm_index_repository",
        "bhm_index_status",
        "bhm_list_projects",
        "bhm_watch_repository",
        "bhm_search_graph",
        "bhm_search_code",
        "bhm_get_code_snippet",
        "bhm_export_graph_artifact",
        "bhm_verify_graph_artifact",
        "bhm_plan_graph_artifact_promotion",
        "bhm_query_graph",
        "bhm_query_graph_dsl",
        "bhm_get_graph_schema",
        "bhm_check_index_coverage",
        "bhm_get_architecture",
        "bhm_resolve_packages",
        "bhm_dependency_provenance",
        "bhm_type_references",
        "bhm_bicep_module_resolution",
        "bhm_trace_path",
        "bhm_trace_graph",
        "bhm_change_impact",
        "bhm_change_impact_preview",
    }
)

# P3.2 attach budget: frequent, low-surprise operations only.  A new tool must
# earn exposure here instead of becoming visible merely by being registered.
CORE_TOOL_NAMES = frozenset(
    {
        "bhm_health",
        "bhm_explain_retrieval",
        "bhm_search",
        "bhm_context_compile",
        "bhm_get_memory",
        "bhm_remember",
        "bhm_forget_preview",
        "bhm_task_open",
        "bhm_task_close",
        "bhm_memory_used",
        "bhm_slot_get",
        "bhm_slot_set",
        # Public, bounded CBM-compatible code-tools.  These are read-only
        # except for bhm_index_repository when apply=true is explicit.
        "bhm_index_repository",
        "bhm_index_status",
        "bhm_list_projects",
        "bhm_watch_repository",
        "bhm_search_graph",
        "bhm_search_code",
        "bhm_get_code_snippet",
        "bhm_export_graph_artifact",
        "bhm_verify_graph_artifact",
        "bhm_plan_graph_artifact_promotion",
        "bhm_query_graph",
        "bhm_query_graph_dsl",
        "bhm_get_graph_schema",
        "bhm_check_index_coverage",
        "bhm_get_architecture",
        "bhm_resolve_packages",
        "bhm_dependency_provenance",
        "bhm_type_references",
        "bhm_bicep_module_resolution",
        "bhm_trace_path",
        "bhm_trace_graph",
        "bhm_change_impact",
        "bhm_change_impact_preview",
    }
)

EXTENDED_PUBLIC_TOOL_NAMES = frozenset(GOVERNANCE_PUBLIC_TOOL_NAMES - CORE_TOOL_NAMES)

_SURFACE_ALIASES = {
    "core": McpSurface.CORE,
    "public": McpSurface.CORE,
    "stable": McpSurface.CORE,
    "admin": McpSurface.ADMIN,
    "operator": McpSurface.ADMIN,
    "internal": McpSurface.ADMIN,
}


def resolve_mcp_surface(value: str | McpSurface | None = None) -> McpSurface:
    """Resolve a configured surface, failing closed to the public catalog."""

    if isinstance(value, McpSurface):
        return value
    raw_value = os.getenv(MCP_SURFACE_ENV, "core") if value is None else value
    normalized = str(raw_value).strip().lower()
    return _SURFACE_ALIASES.get(normalized, McpSurface.CORE)


def is_core_tool(tool_name: str) -> bool:
    """Return whether ``tool_name`` is explicitly approved for core."""

    return str(tool_name).strip() in CORE_TOOL_NAMES


def is_tool_allowed(tool_name: str, surface: McpSurface | str | None = None) -> bool:
    """Check publication permission before dispatching an MCP tool call."""

    resolved_surface = resolve_mcp_surface(surface)
    return resolved_surface is McpSurface.ADMIN or is_core_tool(tool_name)


def requires_admin_capability(tool_name: str, surface: McpSurface | str | None = None) -> bool:
    """Return whether a dispatch needs the operator capability token."""

    return resolve_mcp_surface(surface) is McpSurface.ADMIN and not is_core_tool(tool_name)


def _tool_name(tool: Any) -> str:
    if isinstance(tool, str):
        return tool.strip()
    return str(getattr(tool, "name", "") or "").strip()


def filter_tools(tools: list[Any], surface: McpSurface | str | None = None) -> list[Any]:
    """Filter FastMCP tool models while preserving registry ordering."""

    resolved_surface = resolve_mcp_surface(surface)
    if resolved_surface is McpSurface.ADMIN:
        return list(tools)
    return [tool for tool in tools if is_core_tool(_tool_name(tool))]


def partition_tool_names(tool_names: list[str] | tuple[str, ...] | set[str]) -> dict[str, list[str]]:
    """Return deterministic core/admin partitions for a registered catalog."""

    from .mcp_registration_groups import partition_registration_groups

    names = [str(name).strip() for name in tool_names]
    groups = partition_registration_groups(names)
    return {
        "core": groups["core"],
        "admin": sorted(groups["domain"] + groups["admin"]),
    }


def catalog_report(tool_names: list[str] | tuple[str, ...] | set[str]) -> dict[str, Any]:
    """Build a small static validation report for a registered MCP catalog."""

    names = [str(name).strip() for name in tool_names]
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    registered = set(names)
    partitions = partition_tool_names(names)
    from .mcp_registration_groups import registration_group_report

    registration_groups = registration_group_report(names)
    return {
        "registered_count": len(names),
        "core_count": len(partitions["core"]),
        "admin_count": len(partitions["admin"]),
        "approved_core_count": len(CORE_TOOL_NAMES),
        "extended_public_count": len(EXTENDED_PUBLIC_TOOL_NAMES & registered),
        "missing_core": sorted(CORE_TOOL_NAMES - registered),
        "missing_extended_public": sorted(EXTENDED_PUBLIC_TOOL_NAMES - registered),
        "duplicates": duplicates,
        "core": partitions["core"],
        "admin": partitions["admin"],
        "registration_groups": registration_groups,
    }
