from __future__ import annotations

from typing import Annotated, Any
from pydantic import Field

from . import bhm_mcp_base as _base
from .bhm_mcp_base import (
    BhmBatchLinkItem as BhmBatchLinkItem,
    BhmBatchUpsertItem as BhmBatchUpsertItem,
    DEFAULT_PROJECT as DEFAULT_PROJECT,
    MemoryClass as MemoryClass,
    MemoryEventRole as MemoryEventRole,
    MemoryMetadata as MemoryMetadata,
    TAXONOMY_METADATA_HINT as TAXONOMY_METADATA_HINT,
    _batch_link_item_payload as _batch_link_item_payload,
    _batch_upsert_item_payload as _batch_upsert_item_payload,
    _jsonable_or_text as _jsonable_or_text,
    _mcp_authority_rejection as _mcp_authority_rejection,
    _metadata_payload as _metadata_payload,
    _parse_csv as _parse_csv,
    mcp as mcp,
)

from .bhm_mcp_admin_ops import (
    bhm_admin_export as bhm_admin_export,
    bhm_admin_import_apply as bhm_admin_import_apply,
    bhm_admin_import_preview as bhm_admin_import_preview,
    bhm_agent_activity_rollup as bhm_agent_activity_rollup,
    bhm_alias_resolve as bhm_alias_resolve,
    bhm_alias_stats as bhm_alias_stats,
    bhm_artifact_batch_delete as bhm_artifact_batch_delete,
    bhm_artifact_batch_relink as bhm_artifact_batch_relink,
    bhm_artifact_batch_restore as bhm_artifact_batch_restore,
    bhm_artifact_delete as bhm_artifact_delete,
    bhm_artifact_integrity_audit as bhm_artifact_integrity_audit,
    bhm_artifact_list_by_type as bhm_artifact_list_by_type,
    bhm_artifact_restore as bhm_artifact_restore,
    bhm_artifact_usage_stats as bhm_artifact_usage_stats,
    bhm_batch_archive_memories as bhm_batch_archive_memories,
    bhm_batch_attach_source_refs as bhm_batch_attach_source_refs,
    bhm_batch_delete_memories as bhm_batch_delete_memories,
    bhm_batch_link as bhm_batch_link,
    bhm_batch_link_memories as bhm_batch_link_memories,
    bhm_batch_unlink_memories as bhm_batch_unlink_memories,
    bhm_batch_upsert as bhm_batch_upsert,
    bhm_batch_upsert_memories as bhm_batch_upsert_memories,
    bhm_delete_memory_hard as bhm_delete_memory_hard,
    bhm_entity_catalog_get as bhm_entity_catalog_get,
    bhm_entity_catalog_rebuild as bhm_entity_catalog_rebuild,
    bhm_entity_extract as bhm_entity_extract,
    bhm_entity_link_memories as bhm_entity_link_memories,
    bhm_entity_search as bhm_entity_search,
    bhm_integrity_audit as bhm_integrity_audit,
    bhm_integrity_repair_strict as bhm_integrity_repair_strict,
    bhm_link_cycle_detect as bhm_link_cycle_detect,
    bhm_link_graph_stats as bhm_link_graph_stats,
    bhm_link_orphan_scan as bhm_link_orphan_scan,
    bhm_list_archived_memories as bhm_list_archived_memories,
    bhm_memory_alias_add as bhm_memory_alias_add,
    bhm_memory_alias_remove as bhm_memory_alias_remove,
    bhm_memory_changelog as bhm_memory_changelog,
    bhm_memory_compact as bhm_memory_compact,
    bhm_memory_compaction_report as bhm_memory_compaction_report,
    bhm_memory_diff as bhm_memory_diff,
    bhm_memory_gc_candidates as bhm_memory_gc_candidates,
    bhm_memory_merge_preview as bhm_memory_merge_preview,
    bhm_memory_normalize_metadata as bhm_memory_normalize_metadata,
    bhm_memory_redact as bhm_memory_redact,
    bhm_memory_restore_batch as bhm_memory_restore_batch,
    bhm_memory_restore_from_archive as bhm_memory_restore_from_archive,
    bhm_memory_restore_hard_deleted_preview as bhm_memory_restore_hard_deleted_preview,
    bhm_memory_review_queue as bhm_memory_review_queue,
    bhm_memory_schema_validate as bhm_memory_schema_validate,
    bhm_memory_staleness_report as bhm_memory_staleness_report,
    bhm_memory_triage_queue as bhm_memory_triage_queue,
    bhm_memory_type_migrate as bhm_memory_type_migrate,
    bhm_memory_usage_stats as bhm_memory_usage_stats,
    bhm_ontology_quarantine_list as bhm_ontology_quarantine_list,
    bhm_orphan_artifact_relink as bhm_orphan_artifact_relink,
    bhm_overlap_cleanup_apply as bhm_overlap_cleanup_apply,
    bhm_overlap_report as bhm_overlap_report,
    bhm_policy_enforce_memory as bhm_policy_enforce_memory,
    bhm_policy_guard as bhm_policy_guard,
    bhm_policy_profile_get as bhm_policy_profile_get,
    bhm_policy_profile_set as bhm_policy_profile_set,
    bhm_project_map_compare as bhm_project_map_compare,
    bhm_project_memory_heatmap as bhm_project_memory_heatmap,
    bhm_project_similarity_report as bhm_project_similarity_report,
    bhm_project_summary_compare as bhm_project_summary_compare,
    bhm_project_summary_get as bhm_project_summary_get,
    bhm_project_summary_list as bhm_project_summary_list,
    bhm_project_summary_pin as bhm_project_summary_pin,
    bhm_project_summary_refresh_all as bhm_project_summary_refresh_all,
    bhm_query_suggestions as bhm_query_suggestions,
    bhm_rebuild_project_summary as bhm_rebuild_project_summary,
    bhm_recent_failures_feed as bhm_recent_failures_feed,
    bhm_reindex_memory_metadata as bhm_reindex_memory_metadata,
    bhm_relation_apply_suggestions as bhm_relation_apply_suggestions,
    bhm_relation_confidence_set as bhm_relation_confidence_set,
    bhm_relation_prune_low_quality as bhm_relation_prune_low_quality,
    bhm_relation_suggest as bhm_relation_suggest,
    bhm_relation_vote_quality as bhm_relation_vote_quality,
    bhm_repair_live_indexes as bhm_repair_live_indexes,
    bhm_review_queue_apply as bhm_review_queue_apply,
    bhm_schema_upgrade_all as bhm_schema_upgrade_all,
    bhm_schema_validate_strict as bhm_schema_validate_strict,
    bhm_search_by_source_ref as bhm_search_by_source_ref,
    bhm_search_by_upsert_key as bhm_search_by_upsert_key,
    bhm_search_hybrid as bhm_search_hybrid,
    bhm_secret_scan_existing_memories as bhm_secret_scan_existing_memories,
    bhm_source_refs_detach as bhm_source_refs_detach,
    bhm_source_refs_replace as bhm_source_refs_replace,
    bhm_triage_queue_apply as bhm_triage_queue_apply,
    bhm_validation_trend_report as bhm_validation_trend_report,
)
from .bhm_mcp_admin_governance import (
    bhm_consolidation_change_set_preview as bhm_consolidation_change_set_preview,
    bhm_consolidation_change_set_review as bhm_consolidation_change_set_review,
    bhm_context_tier_promotion_rollback as bhm_context_tier_promotion_rollback,
    bhm_governed_consolidation_apply as bhm_governed_consolidation_apply,
    bhm_governed_consolidation_create as bhm_governed_consolidation_create,
    bhm_governed_consolidation_decide as bhm_governed_consolidation_decide,
    bhm_governed_consolidation_dry_run as bhm_governed_consolidation_dry_run,
    bhm_governed_consolidation_inspect as bhm_governed_consolidation_inspect,
    bhm_governed_consolidation_list as bhm_governed_consolidation_list,
    bhm_governed_consolidation_status as bhm_governed_consolidation_status,
    bhm_governed_consolidation_validate as bhm_governed_consolidation_validate,
    bhm_governed_semantic_proposal as bhm_governed_semantic_proposal,
    bhm_governed_semantic_shadow_metrics as bhm_governed_semantic_shadow_metrics,
    bhm_shared_memory_policy_preflight as bhm_shared_memory_policy_preflight,
    bhm_shared_memory_read as bhm_shared_memory_read,
    bhm_utility_feedback_consolidation_preview as bhm_utility_feedback_consolidation_preview,
    bhm_utility_feedback_record as bhm_utility_feedback_record,
    bhm_utility_feedback_report as bhm_utility_feedback_report,
)

@mcp.tool(name="bhm_remember", description="Writes memory as operator when the MCP HTTP session carries a valid x-bhm-admin-capability header (same path as REST /bhm/remember); otherwise rejects as non-authoritative ingress.")
def bhm_remember(
    content: str,
    project: str = DEFAULT_PROJECT,
    memory_type: str = "workflow",
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    observed_at: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    open_interval: bool | None = None,
    supersedes_revision_id: str | None = None,
    source_episode_id: str | None = None,
    source_uri: str | None = None,
    source_digest: str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    metadata: MemoryMetadata | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "content": content,
        "project": project,
        "type": memory_type,
        "memory_class": memory_class.value if memory_class else None,
        "event_role": event_role.value if event_role else None,
        "concepts": concepts or [],
        "files": files or [],
    }
    for key, value in {
        "observed_at": observed_at,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "open_interval": open_interval,
        "supersedes_revision_id": supersedes_revision_id,
        "source_episode_id": source_episode_id,
        "source_uri": source_uri,
        "source_digest": source_digest,
    }.items():
        if value is not None:
            body[key] = value
    if metadata is not None:
        body["metadata"] = _metadata_payload(metadata)
    return _mcp_authority_rejection(project=project, payload=body)


@mcp.tool(name="bhm_profile", description="Get compact BHM profile stats for a project.")
def bhm_profile(project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _base._get("/bhm/profile", {"project": project})


@mcp.tool(name="bhm_insights", description="Get compact BHM insight/checkpoint summary for a project.")
def bhm_insights(project: str = DEFAULT_PROJECT, limit: int = 5) -> dict[str, Any]:
    return _base._get("/bhm/insights", {"project": project, "limit": limit})


@mcp.tool(name="bhm_lessons_create", description="Create a lesson entry in BHM.")
def bhm_lessons_create(
    content: str,
    project: str = DEFAULT_PROJECT,
    context: str = "",
    confidence: float = 0.7,
    tags_csv: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/lessons",
        {
            "content": content,
            "project": project,
            "context": context,
            "confidence": confidence,
            "tags": _parse_csv(tags_csv) or [],
        },
    )


@mcp.tool(name="bhm_lessons_search", description="Search BHM lessons for a project.")
def bhm_lessons_search(query: str, project: str = DEFAULT_PROJECT, limit: int = 5) -> dict[str, Any]:
    return _base._post("/bhm/lessons/search", {"query": query, "project": project, "limit": limit})


@mcp.tool(name="bhm_observe", description="Send a compact observe event into BHM.")
def bhm_observe(
    hook_type: Annotated[str, Field(min_length=1)],
    session_id: Annotated[str, Field(min_length=1)],
    cwd: str = "",
    project: str = DEFAULT_PROJECT,
    data_json: str | None = None,
    timestamp: str | None = None,
    parent_event_id: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/observe",
        {
            "hookType": hook_type,
            "sessionId": session_id,
            "project": project,
            "cwd": cwd,
            "timestamp": timestamp,
            "parentEventId": parent_event_id,
            "data": _jsonable_or_text(data_json),
        },
    )


@mcp.tool(name="bhm_slot_list", description="List BHM slots for a project.")
def bhm_slot_list(project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _base._get("/bhm/slots", {"project": project})


@mcp.tool(name="bhm_slot_get", description="Get a single BHM slot by label.")
def bhm_slot_get(label: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _base._get("/bhm/slot", {"project": project, "label": label})


@mcp.tool(name="bhm_slot_set", description="Create or overwrite a BHM slot.")
def bhm_slot_set(
    label: str,
    content: str,
    project: str = DEFAULT_PROJECT,
    size_limit: int = 2000,
    description: str = "",
    pinned: bool = True,
    scope: str = "project",
) -> dict[str, Any]:
    return _base._post(
        "/bhm/slot",
        {
            "label": label,
            "content": content,
            "sizeLimit": size_limit,
            "description": description,
            "pinned": pinned,
            "scope": scope,
            "project": project,
        },
    )


@mcp.tool(name="bhm_slot_append", description="Append text to a BHM slot.")
def bhm_slot_append(label: str, text: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _base._post("/bhm/slot/append", {"label": label, "text": text, "project": project})


@mcp.tool(name="bhm_slot_replace", description="Replace BHM slot content.")
def bhm_slot_replace(label: str, content: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _base._post("/bhm/slot/replace", {"label": label, "content": content, "project": project})


@mcp.tool(name="bhm_slot_delete", description="Delete a BHM slot.")
def bhm_slot_delete(label: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _base._delete("/bhm/slot", {"label": label, "project": project})


@mcp.tool(name="bhm_slot_reflect", description="Reflect a BHM slot.")
def bhm_slot_reflect(label: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _base._post("/bhm/slot/reflect", {"label": label, "project": project})

