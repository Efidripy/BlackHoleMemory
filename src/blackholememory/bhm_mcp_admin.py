from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import Field

from . import bhm_mcp_base as _base
from .bhm_mcp_base import (
    BhmBatchLinkItem,
    BhmBatchUpsertItem,
    DEFAULT_PROJECT,
    TAXONOMY_METADATA_HINT,
    _jsonable_or_text,
    _metadata_payload,
    _parse_csv,
    _batch_link_item_payload,
    _batch_upsert_item_payload,
    _mcp_authority_rejection,
    mcp,
    MemoryClass,
    MemoryEventRole,
    MemoryMetadata,
)

@mcp.tool(name="bhm_query_suggestions", description="Suggest useful memory queries for a project based on current live-memory contents.")
def bhm_query_suggestions(project: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if project:
        params["project"] = project
    return _base._get("/bhm/query-suggestions", params)


@mcp.tool(name="bhm_delete_memory_hard", description="Hard-delete a live BHM memory and remove dependent canonical artifact references.")
def bhm_delete_memory_hard(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._delete_json("/bhm/memory/hard", {"id": id, "project": project})


@mcp.tool(name="bhm_source_refs_detach", description="Detach selected canonical source references from a live BHM memory.")
def bhm_source_refs_detach(id: str, refs_csv: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/source-refs/detach", {"id": id, "project": project, "refs": _parse_csv(refs_csv) or []})


@mcp.tool(name="bhm_source_refs_replace", description="Replace canonical source references on a live BHM memory.")
def bhm_source_refs_replace(id: str, refs_csv: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/source-refs/replace", {"id": id, "project": project, "refs": _parse_csv(refs_csv) or []})


@mcp.tool(name="bhm_memory_restore_from_archive", description="Restore an archived live BHM memory back to active state.")
def bhm_memory_restore_from_archive(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/restore", {"id": id, "project": project})


@mcp.tool(name="bhm_batch_upsert", description="Reject direct MCP authority writes; use a future typed review and promotion flow.")
def bhm_batch_upsert(items: list[BhmBatchUpsertItem], project: str | None = None) -> dict[str, Any]:
    payload = {"project": project, "items": [_batch_upsert_item_payload(item) for item in items]}
    return _mcp_authority_rejection(project=project, payload=payload)


@mcp.tool(name="bhm_batch_link", description=f"Batch create explicit memory links with typed item objects. {TAXONOMY_METADATA_HINT}")
def bhm_batch_link(items: list[BhmBatchLinkItem], project: str | None = None) -> dict[str, Any]:
    return _base._post(
        "/bhm/memories/batch-link",
        {"project": project, "items": [_batch_link_item_payload(item) for item in items]},
    )


@mcp.tool(name="bhm_batch_upsert_memories", description="Reject direct MCP authority writes; compatibility wrapper for bhm_batch_upsert.")
def bhm_batch_upsert_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    payload = {"project": project, "items": _jsonable_or_text(items_json) or []}
    return _mcp_authority_rejection(project=project, payload=payload)


@mcp.tool(name="bhm_batch_attach_source_refs", description="Batch attach canonical source references to multiple live BHM memories.")
def bhm_batch_attach_source_refs(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/source-refs/batch", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_batch_archive_memories", description="Batch archive multiple live BHM memories.")
def bhm_batch_archive_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memories/batch-archive", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_batch_delete_memories", description="Batch delete multiple live BHM memories from the live store.")
def bhm_batch_delete_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memories/batch-delete", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_batch_link_memories", description="Compatibility JSON wrapper for bhm_batch_link.")
def bhm_batch_link_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memories/batch-link", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_batch_unlink_memories", description="Batch remove explicit memory links.")
def bhm_batch_unlink_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memories/batch-unlink", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_repair_live_indexes", description="Repair scoped canonical live-memory links and optional orphan artifacts.")
def bhm_repair_live_indexes(project: str | None = None, aggregate: bool = False, remove_orphan_links: bool = True, remove_orphan_artifacts: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/repair-live-indexes", {"project": project, "aggregate": aggregate, "remove_orphan_links": remove_orphan_links, "remove_orphan_artifacts": remove_orphan_artifacts})


@mcp.tool(name="bhm_integrity_audit", description="Read-only integrity audit helper. Prefer bhm_integrity_repair_strict for operator repair passes and gate-level maintenance.")
def bhm_integrity_audit(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/integrity-audit", {"project": project})


@mcp.tool(name="bhm_memory_diff", description="Diff two live BHM memories by content lines.")
def bhm_memory_diff(left_id: str, right_id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/diff", {"left_id": left_id, "right_id": right_id, "project": project})


@mcp.tool(name="bhm_rebuild_project_summary", description="Rebuild a compact project summary from project map, checkpoint, task context, risks, and validation snapshot.")
def bhm_rebuild_project_summary(project: str, upsert_key: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/project-summary/rebuild", {"project": project, "upsert_key": upsert_key})


@mcp.tool(name="bhm_project_summary_get", description="Get the current canonical project summary memory for a project.")
def bhm_project_summary_get(project: str) -> dict[str, Any]:
    return _base._get("/bhm/project-summary", {"project": project})


@mcp.tool(name="bhm_project_summary_pin", description="Pin the canonical project summary memory for a project.")
def bhm_project_summary_pin(project: str) -> dict[str, Any]:
    return _base._post("/bhm/project-summary/pin", {"project": project})


@mcp.tool(name="bhm_project_summary_list", description="List canonical project summary memories.")
def bhm_project_summary_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    return _base._post("/bhm/project-summary/list", {"project": project, "limit": limit, "offset": offset})


@mcp.tool(name="bhm_artifact_integrity_audit", description="Audit artifact stores for orphaned memory references.")
def bhm_artifact_integrity_audit(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/artifact-integrity-audit", {"project": project})


@mcp.tool(name="bhm_entity_extract", description="Extract simple entities such as files, endpoints, env vars, and concepts from a live BHM memory.")
def bhm_entity_extract(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/entity-extract", {"id": id, "project": project})


@mcp.tool(name="bhm_relation_suggest", description="Read-only relation suggestion helper. Prefer bhm_relation_apply_suggestions when operators want deterministic apply behavior.")
def bhm_relation_suggest(project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/relation-suggest", {"project": project, "limit": limit})


@mcp.tool(name="bhm_memory_compact", description="Replace a live BHM memory body with a compact summary while retaining provenance.")
def bhm_memory_compact(id: str, summary: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/compact", {"id": id, "project": project, "summary": summary})


@mcp.tool(name="bhm_link_graph_stats", description="Get lightweight stats for the explicit memory-link graph.")
def bhm_link_graph_stats(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/link-graph-stats", {"project": project})


@mcp.tool(name="bhm_reindex_memory_metadata", description="Rebuild derived metadata such as raw titles for live memories.")
def bhm_reindex_memory_metadata(project: str | None = None, aggregate: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/reindex-memory-metadata", {"project": project, "aggregate": aggregate})


@mcp.tool(name="bhm_memory_schema_validate", description="Lightweight single-record schema validation helper. Prefer bhm_schema_validate_strict for operator gates and broader audits.")
def bhm_memory_schema_validate(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/schema-validate", {"id": id, "project": project})


@mcp.tool(name="bhm_memory_type_migrate", description="Migrate a live BHM memory to a new memory type.")
def bhm_memory_type_migrate(id: str, new_type: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/type-migrate", {"id": id, "project": project, "new_type": new_type})


@mcp.tool(name="bhm_search_hybrid", description="Run hybrid search that combines structured advanced search with query suggestions.")
def bhm_search_hybrid(
    query: str,
    project: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "include_archived": include_archived,
        "include_logs": include_logs,
        "limit": limit,
        **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
    }
    if project:
        body["project"] = project
    if memory_class:
        body["memory_class"] = memory_class.value
    if event_role:
        body["event_role"] = event_role.value
    if as_of:
        body["as_of"] = as_of
    if valid_from:
        body["valid_from"] = valid_from
    if valid_to:
        body["valid_to"] = valid_to
    if domain:
        body["domain"] = domain
    if semantic_type:
        body["semantic_type"] = semantic_type
    if priority:
        body["priority"] = priority
    return _base._post("/bhm/search/hybrid", body)


@mcp.tool(name="bhm_search_by_source_ref", description="Search live BHM memories by canonical source reference or file.")
def bhm_search_by_source_ref(ref: str, project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/search/by-source-ref", {"ref": ref, "project": project, "limit": limit})


@mcp.tool(name="bhm_search_by_upsert_key", description="Search live BHM memories by exact upsert key.")
def bhm_search_by_upsert_key(upsert_key: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/search/by-upsert-key", {"upsert_key": upsert_key, "project": project})


@mcp.tool(name="bhm_list_archived_memories", description="List archived live BHM memories for a project.")
def bhm_list_archived_memories(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/memories/archived", params)


@mcp.tool(name="bhm_memory_restore_batch", description="Restore multiple archived live BHM memories in one call.")
def bhm_memory_restore_batch(items_json: str) -> dict[str, Any]:
    return _base._post("/bhm/memory/restore-batch", {"items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_artifact_restore", description="Restore a first-class artifact by reviving or recreating its backing live memory.")
def bhm_artifact_restore(artifact_type: str, artifact_id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/artifact/restore", {"artifact_type": artifact_type, "artifact_id": artifact_id, "project": project})


@mcp.tool(name="bhm_orphan_artifact_relink", description="Relink an orphan first-class artifact to a target live memory id.")
def bhm_orphan_artifact_relink(artifact_type: str, artifact_id: str, target_memory_id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/artifact/relink", {"artifact_type": artifact_type, "artifact_id": artifact_id, "target_memory_id": target_memory_id, "project": project})


@mcp.tool(name="bhm_memory_staleness_report", description="Report stale live memories older than a chosen age threshold.")
def bhm_memory_staleness_report(project: str | None = None, days: int = 30, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/memory/staleness-report", {"project": project, "days": days, "limit": limit})


@mcp.tool(name="bhm_memory_review_queue", description="Build a deterministic review queue for low-quality memories and contradictions.")
def bhm_memory_review_queue(project: str | None = None, limit: int = 20, include_conflicts: bool = True, include_closed: bool = False) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/review-queue",
        {"project": project, "limit": limit, "include_conflicts": include_conflicts, "include_closed": include_closed},
    )


@mcp.tool(name="bhm_memory_triage_queue", description="Build a deterministic triage queue of duplicates, contradictions, and relation suggestions.")
def bhm_memory_triage_queue(project: str | None = None, limit: int = 20, include_closed: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/memory/triage-queue", {"project": project, "limit": limit, "include_closed": include_closed})


@mcp.tool(name="bhm_project_summary_refresh_all", description="Bulk operator helper for refreshing many project summaries. Prefer bhm_rebuild_project_summary for normal single-project flows.")
def bhm_project_summary_refresh_all(projects_csv: str | None = None, project: str | None = None, aggregate: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/project-summary/refresh-all", {"projects": _parse_csv(projects_csv), "project": project, "aggregate": aggregate})


@mcp.tool(name="bhm_relation_apply_suggestions", description="Apply suggested relations above a confidence threshold.")
def bhm_relation_apply_suggestions(project: str | None = None, aggregate: bool = False, min_score: float = 0.65, limit: int = 20, include_relates_to: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/relation/apply-suggestions", {"project": project, "aggregate": aggregate, "min_score": min_score, "limit": limit, "include_relates_to": include_relates_to})


@mcp.tool(name="bhm_memory_merge_preview", description="Preview a merge of two live memories without modifying either record.")
def bhm_memory_merge_preview(project: str, source_id: str, target_id: str) -> dict[str, Any]:
    return _base._post("/bhm/memory/merge-preview", {"project": project, "source_id": source_id, "target_id": target_id})


@mcp.tool(name="bhm_ontology_quarantine_list", description="List bounded content-free ontology relation worklist items for one project. It never admits or modifies a link.")
def bhm_ontology_quarantine_list(project: str, limit: int = 100) -> dict[str, Any]:
    return _base._get("/bhm/ontology/quarantine", {"project": project, "limit": limit})


@mcp.tool(name="bhm_schema_upgrade_all", description="Apply a lightweight schema upgrade pass to all live memories.")
def bhm_schema_upgrade_all(project: str | None = None, aggregate: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/schema/upgrade-all", {"project": project, "aggregate": aggregate})


@mcp.tool(name="bhm_memory_redact", description="Redact secret-like substrings from a live memory body.")
def bhm_memory_redact(
    id: str,
    project: str | None = None,
    patterns_csv: str | None = None,
    replacement: Annotated[str, Field(max_length=120)] = "[REDACTED]",
) -> dict[str, Any]:
    return _base._post("/bhm/memory/redact", {"id": id, "project": project, "patterns": _parse_csv(patterns_csv), "replacement": replacement})


@mcp.tool(name="bhm_secret_scan_existing_memories", description="Scan existing live memories for secret-like content.")
def bhm_secret_scan_existing_memories(project: str | None = None, limit: int = 50) -> dict[str, Any]:
    return _base._post("/bhm/memory/secret-scan", {"project": project, "limit": limit})


@mcp.tool(name="bhm_agent_activity_rollup", description="Get compact activity rollups across checkpoints, handoffs, sessions, and observations.")
def bhm_agent_activity_rollup(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/agent-activity-rollup", {"project": project})


@mcp.tool(name="bhm_project_memory_heatmap", description="Get type, tag, and age-bucket distributions for project live memories.")
def bhm_project_memory_heatmap(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/project-memory-heatmap", {"project": project})


@mcp.tool(name="bhm_relation_confidence_set", description="Set an explicit confidence score on a memory relation link.")
def bhm_relation_confidence_set(source_id: str, target_id: str, relation: str, project: str, confidence: float) -> dict[str, Any]:
    return _base._post("/bhm/relation/confidence", {"source_id": source_id, "target_id": target_id, "relation": relation, "project": project, "confidence": confidence})


@mcp.tool(name="bhm_relation_vote_quality", description="Vote on the quality of a memory relation link.")
def bhm_relation_vote_quality(source_id: str, target_id: str, relation: str, project: str, vote: int, voter: str = "agent") -> dict[str, Any]:
    return _base._post("/bhm/relation/vote-quality", {"source_id": source_id, "target_id": target_id, "relation": relation, "project": project, "vote": vote, "voter": voter})


@mcp.tool(name="bhm_memory_alias_add", description="Add an alias to a live memory for future resolution.")
def bhm_memory_alias_add(id: str, alias: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/alias/add", {"id": id, "alias": alias, "project": project})


@mcp.tool(name="bhm_memory_alias_remove", description="Remove an alias from a live memory.")
def bhm_memory_alias_remove(id: str, alias: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/alias/remove", {"id": id, "alias": alias, "project": project})


@mcp.tool(name="bhm_alias_resolve", description="Resolve an alias to matching live memories.")
def bhm_alias_resolve(alias: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/alias/resolve", {"alias": alias, "project": project})


@mcp.tool(name="bhm_entity_catalog_get", description="Get the current derived entity catalog for a project.")
def bhm_entity_catalog_get(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/entity-catalog/get", {"project": project})


@mcp.tool(name="bhm_entity_catalog_rebuild", description="Rebuild the derived entity catalog for a project.")
def bhm_entity_catalog_rebuild(project: str | None = None, aggregate: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/entity-catalog/rebuild", {"project": project, "aggregate": aggregate})


@mcp.tool(name="bhm_project_summary_compare", description="Compare canonical project summaries between two projects.")
def bhm_project_summary_compare(left_project: str, right_project: str) -> dict[str, Any]:
    return _base._post("/bhm/project-summary/compare", {"left_project": left_project, "right_project": right_project})


@mcp.tool(name="bhm_memory_usage_stats", description="Get compact memory usage stats for a project.")
def bhm_memory_usage_stats(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/usage-stats", {"project": project})


@mcp.tool(name="bhm_recent_failures_feed", description="Get a compact feed of recent failure-like validation or handoff artifacts.")
def bhm_recent_failures_feed(project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/recent-failures-feed", {"project": project, "limit": limit})


@mcp.tool(name="bhm_memory_restore_hard_deleted_preview", description="Preview whether a live memory could be reconstructed after a hard delete.")
def bhm_memory_restore_hard_deleted_preview(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/restore-hard-deleted-preview", {"id": id, "project": project})


@mcp.tool(name="bhm_artifact_delete", description="Delete a first-class artifact, optionally deleting its backing memory too.")
def bhm_artifact_delete(artifact_type: str, artifact_id: str, project: str | None = None, delete_backing_memory: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/artifact/delete", {"artifact_type": artifact_type, "artifact_id": artifact_id, "project": project, "delete_backing_memory": delete_backing_memory})


@mcp.tool(name="bhm_artifact_list_by_type", description="List first-class artifacts by canonical artifact type.")
def bhm_artifact_list_by_type(artifact_type: str, project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    return _base._post("/bhm/artifact/list-by-type", {"artifact_type": artifact_type, "project": project, "limit": limit, "offset": offset})


@mcp.tool(name="bhm_artifact_usage_stats", description="Get usage stats across canonical artifact stores.")
def bhm_artifact_usage_stats(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/artifact/usage-stats", {"project": project})


@mcp.tool(name="bhm_memory_gc_candidates", description="Report live memories that look like good garbage-collection candidates.")
def bhm_memory_gc_candidates(project: str | None = None, stale_days: int = 90, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/memory/gc-candidates", {"project": project, "stale_days": stale_days, "limit": limit})


@mcp.tool(name="bhm_memory_compaction_report", description="Report oversized or log-shaped memories that should be compacted.")
def bhm_memory_compaction_report(project: str | None = None, min_chars: int = 1200, min_lines: int = 25, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/memory/compaction-report", {"project": project, "min_chars": min_chars, "min_lines": min_lines, "limit": limit})


@mcp.tool(name="bhm_link_cycle_detect", description="Detect cycles in the explicit memory-link graph.")
def bhm_link_cycle_detect(project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/link/cycle-detect", {"project": project, "limit": limit})


@mcp.tool(name="bhm_link_orphan_scan", description="Scan the explicit memory-link graph for orphan links.")
def bhm_link_orphan_scan(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/link/orphan-scan", {"project": project})


@mcp.tool(name="bhm_project_map_compare", description="Compare canonical project maps between two projects.")
def bhm_project_map_compare(left_project: str, right_project: str) -> dict[str, Any]:
    return _base._post("/bhm/project-map/compare", {"left_project": left_project, "right_project": right_project})


@mcp.tool(name="bhm_validation_trend_report", description="Get a compact trend report across validation snapshots for a project.")
def bhm_validation_trend_report(project: str, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/validation/trend-report", {"project": project, "limit": limit})


@mcp.tool(name="bhm_entity_search", description="Search the derived entity catalog for a project.")
def bhm_entity_search(query: str, project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/entity/search", {"query": query, "project": project, "limit": limit})


@mcp.tool(name="bhm_entity_link_memories", description="Create explicit links between memories that share a chosen entity.")
def bhm_entity_link_memories(entity: str, project: str, relation: str = "relates_to", limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/entity/link-memories", {"entity": entity, "project": project, "relation": relation, "limit": limit})


@mcp.tool(name="bhm_alias_stats", description="Get alias usage and duplication stats.")
def bhm_alias_stats(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/alias/stats", {"project": project})


@mcp.tool(name="bhm_relation_prune_low_quality", description="Prune low-confidence or low-quality explicit relations.")
def bhm_relation_prune_low_quality(project: str | None = None, aggregate: bool = False, max_confidence: float = 0.5, max_quality_score: float = 2.5, remove_unscored: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/relation/prune-low-quality", {"project": project, "aggregate": aggregate, "max_confidence": max_confidence, "max_quality_score": max_quality_score, "remove_unscored": remove_unscored})


@mcp.tool(name="bhm_project_similarity_report", description="Find projects with the most overlap in concepts and files.")
def bhm_project_similarity_report(project: str, limit: int = 10) -> dict[str, Any]:
    return _base._post("/bhm/project-similarity-report", {"project": project, "limit": limit})


@mcp.tool(name="bhm_memory_changelog", description="Read the explicit and inferred changelog for a live memory.")
def bhm_memory_changelog(id: str, project: str | None = None, limit: int = 50) -> dict[str, Any]:
    return _base._post("/bhm/memory/changelog", {"id": id, "project": project, "limit": limit})


@mcp.tool(name="bhm_review_queue_apply", description="Apply deterministic review actions to selected quality or contradiction queue items.")
def bhm_review_queue_apply(
    project: str | None = None,
    limit: int = 20,
    mark_needs_review: bool = True,
    auto_redact_secrets: bool = True,
    queue_ids_csv: str | None = None,
    status: Literal["needs_review", "resolved", "dismissed"] = "needs_review",
) -> dict[str, Any]:
    return _base._post(
        "/bhm/review-queue/apply",
        {
            "project": project,
            "limit": limit,
            "mark_needs_review": mark_needs_review,
            "auto_redact_secrets": auto_redact_secrets,
            "queue_ids": _parse_csv(queue_ids_csv) or [],
            "status": status,
        },
    )


@mcp.tool(name="bhm_triage_queue_apply", description="Apply deterministic triage actions from the current triage queue.")
def bhm_triage_queue_apply(project: str | None = None, limit: int = 20, min_score: float = 0.75, include_relates_to: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/triage-queue/apply", {"project": project, "limit": limit, "min_score": min_score, "include_relates_to": include_relates_to})


@mcp.tool(name="bhm_artifact_batch_delete", description="Delete multiple first-class artifacts in one call.")
def bhm_artifact_batch_delete(artifact_type: str, artifact_ids_csv: str, project: str | None = None, delete_backing_memory: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/artifact/batch-delete", {"artifact_type": artifact_type, "artifact_ids": _parse_csv(artifact_ids_csv) or [], "project": project, "delete_backing_memory": delete_backing_memory})


@mcp.tool(name="bhm_artifact_batch_relink", description="Relink multiple first-class artifacts to target memories in one call.")
def bhm_artifact_batch_relink(artifact_type: str, items_json: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/artifact/batch-relink", {"artifact_type": artifact_type, "items": _jsonable_or_text(items_json) or [], "project": project})


@mcp.tool(name="bhm_artifact_batch_restore", description="Restore multiple first-class artifacts in one call.")
def bhm_artifact_batch_restore(artifact_type: str, artifact_ids_csv: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/artifact/batch-restore", {"artifact_type": artifact_type, "artifact_ids": _parse_csv(artifact_ids_csv) or [], "project": project})


@mcp.tool(name="bhm_schema_validate_strict", description="Run strict schema validation across live memories and artifact references.")
def bhm_schema_validate_strict(project: str | None = None, include_archived: bool = True) -> dict[str, Any]:
    return _base._post("/bhm/schema/validate-strict", {"project": project, "include_archived": include_archived})


@mcp.tool(name="bhm_integrity_repair_strict", description="Run a stricter integrity repair pass with orphan cleanup and metadata normalization.")
def bhm_integrity_repair_strict(project: str | None = None, remove_orphan_links: bool = True, remove_orphan_artifacts: bool = True, normalize_metadata: bool = True) -> dict[str, Any]:
    return _base._post("/bhm/integrity/repair-strict", {"project": project, "remove_orphan_links": remove_orphan_links, "remove_orphan_artifacts": remove_orphan_artifacts, "normalize_metadata": normalize_metadata})


@mcp.tool(name="bhm_memory_normalize_metadata", description="Normalize live-memory metadata fields such as raw_title, files, source_refs, aliases, and changelog.")
def bhm_memory_normalize_metadata(project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/normalize-metadata", {"project": project})


@mcp.tool(name="bhm_admin_export", description="Export canonical live-memory, links, and artifacts to a JSON admin snapshot.")
def bhm_admin_export(project: str | None = None, include_archived: bool = True, include_artifacts: bool = True, export_name: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/admin/export", {"project": project, "include_archived": include_archived, "include_artifacts": include_artifacts, "export_name": export_name})


@mcp.tool(name="bhm_admin_import_preview", description="Preview a JSON admin snapshot before import.")
def bhm_admin_import_preview(path: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/admin/import-preview", {"path": path, "project": project})


@mcp.tool(name="bhm_admin_import_apply", description="Apply a JSON admin snapshot import in upsert or replace mode.")
def bhm_admin_import_apply(path: str, merge_mode: str = "upsert", project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/admin/import-apply", {"path": path, "merge_mode": merge_mode, "project": project})


@mcp.tool(name="bhm_policy_profile_get", description="Get the current canonical BHM policy profile.")
def bhm_policy_profile_get() -> dict[str, Any]:
    return _base._get("/bhm/policy/profile")


@mcp.tool(name="bhm_policy_profile_set", description="Set the canonical BHM policy profile.")
def bhm_policy_profile_set(max_content_chars: int = 8000, max_lines: int = 120, require_project: bool = True, require_memory_type: bool = False, block_secret_like: bool = True, block_raw_logs: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/policy/profile", {"max_content_chars": max_content_chars, "max_lines": max_lines, "require_project": require_project, "require_memory_type": require_memory_type, "block_secret_like": block_secret_like, "block_raw_logs": block_raw_logs})


@mcp.tool(name="bhm_policy_enforce_memory", description="Enforce the current policy profile against a specific live memory.")
def bhm_policy_enforce_memory(id: str, project: str | None = None, auto_redact: bool = False) -> dict[str, Any]:
    return _base._post("/bhm/policy/enforce-memory", {"id": id, "project": project, "auto_redact": auto_redact})


@mcp.tool(name="bhm_overlap_report", description="Report likely overlapping or redundant live memories.")
def bhm_overlap_report(project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _base._post("/bhm/overlap/report", {"project": project, "limit": limit})


@mcp.tool(name="bhm_overlap_cleanup_apply", description="Apply deterministic overlap cleanup by merging duplicate candidates.")
def bhm_overlap_cleanup_apply(project: str | None = None, aggregate: bool = False, limit: int = 20, archive_sources: bool = True) -> dict[str, Any]:
    return _base._post("/bhm/overlap/cleanup-apply", {"project": project, "aggregate": aggregate, "limit": limit, "archive_sources": archive_sources})


@mcp.tool(name="bhm_policy_guard", description="Guard a candidate memory payload for secrets, raw logs, oversize content, and missing scope.")
def bhm_policy_guard(content: str, project: str | None = None, memory_type: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/policy-guard", {"content": content, "project": project, "memory_type": memory_type})


@mcp.tool(name="bhm_shared_memory_policy_preflight", description="Evaluate governed shared-memory policy and append a content-free SQLite audit receipt. This tool never reads or writes shared memory.")
def bhm_shared_memory_policy_preflight(
    request_id: Annotated[str, Field(min_length=1, max_length=256)],
    operation: Literal["read", "write", "update", "transition", "delete"],
    visibility: Literal["private/agent", "session", "project", "team", "org/tenant"],
    owner_id: Annotated[str, Field(min_length=1, max_length=256)],
    at: Annotated[str, Field(min_length=20, max_length=64)],
    project: Annotated[str, Field(min_length=1, max_length=256)],
    memory_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
    sensitivity: Literal["public", "internal", "restricted"] = "internal",
    expected_revision: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "operation": operation,
        "visibility": visibility,
        "owner_id": owner_id,
        "at": at,
        "project": project,
        "sensitivity": sensitivity,
    }
    if memory_id is not None:
        body["memory_id"] = memory_id
    if expected_revision is not None:
        body["expected_revision"] = expected_revision
    return _base._post("/bhm/shared-memory/policy/evaluate", body)


@mcp.tool(name="bhm_shared_memory_read", description="Read one active project memory only through the feature-gated governed shared-memory policy. It never writes shared memory, Qdrant, Mem0, or lifecycle state.")
def bhm_shared_memory_read(
    request_id: Annotated[str, Field(min_length=1, max_length=256)],
    visibility: Literal["private/agent", "session", "project", "team", "org/tenant"],
    owner_id: Annotated[str, Field(min_length=1, max_length=256)],
    memory_id: Annotated[str, Field(min_length=1, max_length=256)],
    at: Annotated[str, Field(min_length=20, max_length=64)],
    project: Annotated[str, Field(min_length=1, max_length=256)],
    sensitivity: Literal["public", "internal", "restricted"] = "internal",
    expected_revision: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "operation": "read",
        "visibility": visibility,
        "owner_id": owner_id,
        "memory_id": memory_id,
        "at": at,
        "project": project,
        "sensitivity": sensitivity,
    }
    if expected_revision is not None:
        body["expected_revision"] = expected_revision
    return _base._post("/bhm/shared-memory/read", body)


@mcp.tool(name="bhm_utility_feedback_record", description="Append one caller-bound, immutable utility signal for an existing project memory. This never changes lifecycle or projections.")
def bhm_utility_feedback_record(
    event_id: Annotated[str, Field(min_length=1, max_length=160)],
    memory_id: Annotated[str, Field(min_length=1, max_length=160)],
    event_type: Literal["retrieved", "used", "accepted", "dismissed", "corrected", "contradicted"],
    observed_at: Annotated[str, Field(min_length=20, max_length=64)],
    request_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    project: Annotated[str, Field(min_length=1, max_length=160)],
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0)] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": event_id,
        "memory_id": memory_id,
        "event_type": event_type,
        "observed_at": observed_at,
        "request_digest": request_digest,
        "project": project,
    }
    if confidence is not None:
        body["confidence"] = confidence
    return _base._post("/bhm/utility-feedback/event", body)


@mcp.tool(name="bhm_utility_feedback_report", description="Return a deterministic read-only utility report for one project; low scores never trigger lifecycle changes.")
def bhm_utility_feedback_report(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    half_life_days: Annotated[float, Field(ge=0.25, le=3_650)] = 30.0,
    min_samples: Annotated[int, Field(ge=1, le=10_000)] = 3,
) -> dict[str, Any]:
    return _base._get(
        "/bhm/utility-feedback/report",
        {
            "project": project,
            "as_of": as_of,
            "half_life_days": half_life_days,
            "min_samples": min_samples,
        },
    )


@mcp.tool(name="bhm_utility_feedback_consolidation_preview", description="Return a bounded, content-free operator review worklist from immutable utility feedback. It never applies merge, archive, tombstone, or ranking changes.")
def bhm_utility_feedback_consolidation_preview(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    half_life_days: Annotated[float, Field(ge=0.25, le=3_650)] = 30.0,
    min_samples: Annotated[int, Field(ge=1, le=10_000)] = 3,
    max_proposals: Annotated[int, Field(ge=1, le=256)] = 64,
) -> dict[str, Any]:
    return _base._get(
        "/bhm/utility-feedback/consolidation-preview",
        {
            "project": project,
            "as_of": as_of,
            "half_life_days": half_life_days,
            "min_samples": min_samples,
            "max_proposals": max_proposals,
        },
    )


@mcp.tool(name="bhm_consolidation_change_set_preview", description="Build a bounded, snapshot-bound, content-free consolidation change-set preview. Operator approval and typed dry-run are still required; this tool never applies changes.")
def bhm_consolidation_change_set_preview(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    candidates: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=128)],
    max_actions: Annotated[int, Field(ge=1, le=128)] = 64,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/consolidation/change-set/preview",
        {
            "project": project,
            "as_of": as_of,
            "candidates": candidates,
            "max_actions": max_actions,
        },
    )


@mcp.tool(name="bhm_consolidation_change_set_review", description="Record an admin-gated, append-only decision over a freshly regenerated consolidation change-set. Approval never applies a lifecycle, SQLite, Qdrant, Mem0, model, or ranker change.")
def bhm_consolidation_change_set_review(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    candidates: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=128)],
    change_set: dict[str, Any],
    review_id: Annotated[str, Field(min_length=1, max_length=96)],
    decision: Literal["approved_no_apply", "rejected", "deferred"],
    action_ids: Annotated[list[str], Field(min_length=1, max_length=128)],
    reviewed_at: Annotated[str, Field(min_length=20, max_length=64)],
    rationale_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    max_actions: Annotated[int, Field(ge=1, le=128)] = 64,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/consolidation/change-set/review",
        {
            "project": project,
            "as_of": as_of,
            "candidates": candidates,
            "change_set": change_set,
            "review_id": review_id,
            "decision": decision,
            "action_ids": action_ids,
            "reviewed_at": reviewed_at,
            "rationale_digest": rationale_digest,
            "max_actions": max_actions,
        },
    )


@mcp.tool(name="bhm_governed_consolidation_status", description="Report the disabled, proposal-only, approval-gated, policy-auto-reviewed, or degraded governed consolidation state. It never writes SQLite, Mem0, or Qdrant.")
def bhm_governed_consolidation_status() -> dict[str, Any]:
    return _base._get("/bhm/governed-consolidation/status")


@mcp.tool(name="bhm_governed_consolidation_create", description="Create or replay a bounded same-project consolidation proposal. It never applies a memory lifecycle mutation or writes Qdrant/Mem0.")
def bhm_governed_consolidation_create(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    memory_ids: Annotated[list[str], Field(min_length=1, max_length=32)],
    operation: Literal["no_op", "create", "revise", "supersede", "archive", "link"] = "create",
    candidate: dict[str, Any] | None = None,
    reason: Annotated[str | None, Field(max_length=480)] = None,
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.75,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/governed-consolidation/proposals",
        {"project": project, "memory_ids": memory_ids, "operation": operation, "candidate": candidate, "reason": reason, "confidence": confidence},
    )


@mcp.tool(name="bhm_governed_semantic_proposal", description="Retrieve up to 20 semantic candidates, re-read their canonical SQLite revisions, and return or explicitly store one local-model proposal. Preview never mutates; an explicitly stored proposal may enter the separately feature-gated deterministic auto-review/apply path. It never writes Qdrant/Mem0 directly.")
def bhm_governed_semantic_proposal(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    query: Annotated[str, Field(min_length=1, max_length=480)],
    limit: Annotated[int, Field(ge=1, le=20)] = 12,
    store_proposal: bool = False,
    include_historical: bool = False,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/governed-consolidation/semantic-proposals",
        {
            "project": project,
            "query": query,
            "limit": limit,
            "store_proposal": store_proposal,
            "include_historical": include_historical,
        },
    )


@mcp.tool(name="bhm_governed_semantic_shadow_metrics", description="Return content-free shadow metrics for stored local semantic editor proposals. It is read-only and never evaluates quality from model self-claims.")
def bhm_governed_semantic_shadow_metrics(
    project: Annotated[str, Field(min_length=1, max_length=160)],
) -> dict[str, Any]:
    return _base._get("/bhm/governed-consolidation/semantic-shadow-metrics", {"project": project})


@mcp.tool(name="bhm_governed_consolidation_list", description="List bounded project-scoped governed consolidation proposals. Read-only; proposal contents remain operator-scoped.")
def bhm_governed_consolidation_list(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    status: Annotated[str | None, Field(max_length=16)] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    return _base._get("/bhm/governed-consolidation/proposals", {"project": project, "status": status, "limit": limit})


@mcp.tool(name="bhm_governed_consolidation_inspect", description="Inspect one project-scoped governed consolidation proposal and its redacted approval state. Read-only.")
def bhm_governed_consolidation_inspect(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
) -> dict[str, Any]:
    return _base._get(f"/bhm/governed-consolidation/proposals/{proposal_id}", {"project": project})


@mcp.tool(name="bhm_governed_consolidation_validate", description="Revalidate an existing proposal's project, current revision and content digests against SQLite authority. It never writes.")
def bhm_governed_consolidation_validate(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
) -> dict[str, Any]:
    return _base._get(f"/bhm/governed-consolidation/proposals/{proposal_id}/validate", {"project": project})


@mcp.tool(name="bhm_governed_consolidation_dry_run", description="Return the exact apply prerequisites and current stale result for a proposal. It never writes.")
def bhm_governed_consolidation_dry_run(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
) -> dict[str, Any]:
    return _base._post("/bhm/governed-consolidation/proposals/dry-run", {"project": project, "proposal_id": proposal_id})


@mcp.tool(name="bhm_governed_consolidation_decide", description="Admin-only: record one approve or reject decision for a project-scoped proposal. Semantic approval additionally requires an exact evidence-bound operator attestation. Approval alone never applies memory state.")
def bhm_governed_consolidation_decide(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
    decision: Literal["approve", "reject"],
    semantic_attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"project": project, "proposal_id": proposal_id, "decision": decision}
    if semantic_attestation is not None:
        payload["semantic_attestation"] = semantic_attestation
    return _base._post("/bhm/governed-consolidation/proposals/decision", payload)


@mcp.tool(name="bhm_governed_consolidation_apply", description="Admin-only: apply exactly one approved proposal after explicit apply=true and matching proposal confirmation. Revalidates SQLite then uses the outbox; never writes Qdrant/Mem0 directly.")
def bhm_governed_consolidation_apply(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
    confirmation: Annotated[str, Field(min_length=8, max_length=96)],
    apply: bool = False,
) -> dict[str, Any]:
    return _base._post("/bhm/governed-consolidation/proposals/apply", {"project": project, "proposal_id": proposal_id, "confirmation": confirmation, "apply": apply})


@mcp.tool(name="bhm_context_tier_promotion_rollback", description="Admin-only: roll back one durable tier-promotion receipt after exact project-scoped confirmation and revision-CAS. Uses the existing outbox and never writes Mem0/Qdrant directly.")
def bhm_context_tier_promotion_rollback(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    candidate_id: Annotated[str, Field(min_length=8, max_length=160)],
    confirmation: Annotated[str, Field(min_length=8, max_length=160)],
    apply: bool = False,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/context-tier-promotion/rollback",
        {"project": project, "candidate_id": candidate_id, "confirmation": confirmation, "apply": apply},
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

