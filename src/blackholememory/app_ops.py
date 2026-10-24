"""Domain module app_ops: route registration and ops coordination (FIX-017)."""

from __future__ import annotations

from datetime import datetime, timezone
from fastapi import HTTPException
from fastapi.responses import FileResponse

from . import app_core
from . import app_core_hit
from . import app_core_hook
from . import app_ops_admin
from . import app_ops_entity
from . import app_ops_task
from . import app_ops_task2
from . import app_runtime as rt
from .api_models import (
    EntityLinkMemoriesRequest,
    MemoryLinkRequest,
    PolicyEnforceMemoryRequest,
    ProjectOnlyRequest,
    RelationApplySuggestionsRequest,
    RelationConfidenceRequest,
    RelationPruneLowQualityRequest,
    RelationSuggestRequest,
    RelationVoteRequest,
    ReviewQueueApplyRequest,
    TriageQueueApplyRequest,
)

from .app_ops_review import (
    _CUSTOM_REDACTION_MAX_INPUT_CHARS,
    _CUSTOM_REDACTION_MAX_PATTERN_LENGTH,
    _combined_review_status,
    _detect_conflicts,
    _lint_memory_record,
    _memory_content_sha256,
    _memory_redact,
    _memory_review_queue,
    _policy_enforce_memory,
    _redact_memory_record,
    _review_queue_apply,
    _review_queue_id,
    _review_status,
    _serialize_conflict_candidate,
)
from .app_ops_relation import (
    _active_ontology_schema,
    _append_ontology_quarantine,
    _create_memory_link,
    _entity_link_memories,
    _link_orphan_scan,
    _relation_apply_suggestions,
    _relation_confidence_set,
    _relation_prune_low_quality,
    _relation_suggest,
    _relation_vote_quality,
    _serialize_memory_link,
    _triage_queue_apply,
)

__all__ = (
    "_CUSTOM_REDACTION_MAX_INPUT_CHARS",
    "_CUSTOM_REDACTION_MAX_PATTERN_LENGTH",
    "_active_ontology_schema",
    "_adr_supersede",
    "_append_ontology_quarantine",
    "_combined_review_status",
    "_create_memory_link",
    "_detect_conflicts",
    "_entity_link_memories",
    "_integrity_audit",
    "_link_orphan_scan",
    "_lint_memory_record",
    "_memory_content_sha256",
    "_memory_redact",
    "_memory_review_queue",
    "_policy_enforce_memory",
    "_redact_memory_record",
    "_relation_apply_suggestions",
    "_relation_confidence_set",
    "_relation_prune_low_quality",
    "_relation_suggest",
    "_relation_vote_quality",
    "_review_queue_apply",
    "_review_queue_id",
    "_review_status",
    "_serialize_conflict_candidate",
    "_serialize_memory_link",
    "_triage_queue_apply",
    "bhm_entity_link_memories",
    "bhm_link_orphan_scan",
    "bhm_policy_enforce_memory",
    "bhm_relation_apply_suggestions",
    "bhm_relation_confidence_set",
    "bhm_relation_prune_low_quality",
    "bhm_relation_suggest",
    "bhm_relation_vote_quality",
    "bhm_review_queue_apply",
    "bhm_triage_queue_apply",
    "register",
)


def _adr_supersede(project: str, old_id: str, new_id: str) -> dict:
    adrs = app_ops_task2._load_adrs()
    old_record = next((item for item in adrs if item.get("id") == old_id and item.get("project") == project), None)
    new_record = next((item for item in adrs if item.get("id") == new_id and item.get("project") == project), None)
    if old_record is None:
        raise HTTPException(status_code=404, detail="old ADR not found")
    if new_record is None:
        raise HTTPException(status_code=404, detail="new ADR not found")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    old_record["status"] = "superseded"
    old_record["updated_at"] = now
    old_record.setdefault("metadata", {})["superseded_by"] = new_id
    new_record.setdefault("metadata", {})["supersedes"] = old_id
    new_record["updated_at"] = now
    app_ops_task2._save_adrs(adrs)
    if old_record.get("memory_id") and new_record.get("memory_id"):
        _create_memory_link(
            MemoryLinkRequest(
                source_id=new_record["memory_id"],
                target_id=old_record["memory_id"],
                relation="supersedes",
                project=project,
                metadata={"artifact": "adr"},
            )
        )
    return {"old": old_record, "new": new_record}

def _integrity_audit(project: str | None = None) -> dict:
    canonical_project = rt._canonical_project(project) if project else None
    memories = [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=canonical_project, include_archived=True)
    ]
    memory_ids = {item.get("source_id") for item in memories}
    orphan_links = [
        item for item in rt._load_memory_links()
        if (not canonical_project or app_core_hook._project_matches(item.get("project"), canonical_project))
        and (item.get("source_id") not in memory_ids or item.get("target_id") not in memory_ids)
    ]
    duplicate_upsert_keys = []
    seen: dict[tuple[str, str], str] = {}
    for item in memories:
        upsert_key = (item.get("metadata") or {}).get("upsert_key")
        if not upsert_key:
            continue
        key = (rt._canonical_project(item.get("project")) if item.get("project") else "", upsert_key)
        if key in seen:
            duplicate_upsert_keys.append({"project": key[0], "upsert_key": upsert_key, "ids": [seen[key], item.get("source_id")]})
        else:
            seen[key] = item.get("source_id")
    archived_but_pinned = [
        item.get("source_id") for item in memories
        if app_core._is_archived_memory(item) and bool((item.get("metadata") or {}).get("pinned"))
    ]
    return {
        "project": canonical_project,
        "orphan_links": [_serialize_memory_link(item) for item in orphan_links],
        "duplicate_upsert_keys": duplicate_upsert_keys,
        "archived_but_pinned": archived_but_pinned,
        "ok": len(orphan_links) == 0 and len(duplicate_upsert_keys) == 0 and len(archived_but_pinned) == 0,
    }

def bhm_relation_suggest(request: RelationSuggestRequest) -> dict:
    return _relation_suggest(request)

def bhm_relation_apply_suggestions(request: RelationApplySuggestionsRequest) -> dict:
    return _relation_apply_suggestions(request)

def bhm_relation_confidence_set(request: RelationConfidenceRequest) -> dict:
    link = _relation_confidence_set(request)
    return {"success": True, "link": link}

def bhm_relation_vote_quality(request: RelationVoteRequest) -> dict:
    link = _relation_vote_quality(request)
    return {"success": True, "link": link}

def bhm_link_orphan_scan(request: ProjectOnlyRequest) -> dict:
    return _link_orphan_scan(request.project)

def bhm_entity_link_memories(request: EntityLinkMemoriesRequest) -> dict:
    return _entity_link_memories(request)

def bhm_relation_prune_low_quality(request: RelationPruneLowQualityRequest) -> dict:
    return _relation_prune_low_quality(request)

def bhm_review_queue_apply(request: ReviewQueueApplyRequest) -> dict:
    return _review_queue_apply(request)

def bhm_triage_queue_apply(request: TriageQueueApplyRequest) -> dict:
    return _triage_queue_apply(request)

def bhm_policy_enforce_memory(request: PolicyEnforceMemoryRequest) -> dict:
    return _policy_enforce_memory(request)

def register(app) -> None:
    """Attach this module's routes to the application."""

    app.get("/", response_class=FileResponse)(app_ops_task.index)
    app.get("/favicon.ico", response_class=FileResponse)(app_ops_task.favicon_ico)
    app.get("/favicon.svg", response_class=FileResponse)(app_ops_task.favicon_svg)
    app.get("/static/redoc.standalone.js", response_class=FileResponse)(app_ops_task.redoc_bundle)
    app.get("/static/3d-force-graph.min.js", response_class=FileResponse)(app_ops_task.force_graph_bundle)
    app.get("/static/three.module.min.js", response_class=FileResponse)(app_ops_task.three_module_bundle)
    app.get("/static/three.core.min.js", response_class=FileResponse)(app_ops_task.three_core_bundle)
    app.get("/redoc")(app_ops_task.local_redoc)
    app.post("/bhm/conventions/preview", include_in_schema=False)(app_ops_task.bhm_conventions_preview)
    app.post("/bhm/factories/preview", include_in_schema=False)(app_ops_task.bhm_factories_preview)
    app.post("/bhm/migration/preview", include_in_schema=False)(app_ops_task.bhm_migration_preview)
    app.post("/bhm/security/trust-boundary/preview", include_in_schema=False)(app_ops_task.bhm_security_trust_boundary_preview)
    app.get("/graph/status")(app_ops_task.graph_status)
    app.websocket("/bhm/ws")(app_ops_task.bhm_memory_pulse_ws)
    app.get("/bhm/memory")(app_ops_task.bhm_memory_get)
    app.post("/bhm/forget/preview")(app_ops_entity.bhm_forget_preview)
    app.post("/bhm/forget/apply")(app_ops_entity.bhm_forget_apply)
    app.post("/bhm/recent-activity")(app_ops_task.bhm_recent_activity)
    app.post("/bhm/checkpoint")(app_ops_admin.bhm_checkpoint_create)
    app.get("/bhm/checkpoints")(app_ops_task.bhm_checkpoint_list)
    app.get("/bhm/checkpoint/latest")(app_ops_task.bhm_checkpoint_get_latest)
    app.delete("/bhm/memory")(app_ops_task.bhm_memory_delete)
    app.post("/bhm/adr")(app_ops_task.bhm_adr_create)
    app.get("/bhm/adrs")(app_ops_task.bhm_adr_list)
    app.post("/bhm/adr/supersede")(app_ops_task.bhm_adr_supersede)
    app.post("/bhm/handoff")(app_ops_task.bhm_handoff_create)
    app.get("/bhm/handoffs")(app_ops_task.bhm_handoff_list)
    app.post("/bhm/session-record")(app_ops_task.bhm_session_record_create)
    app.get("/bhm/session-records")(app_ops_task.bhm_session_record_list)
    app.post("/bhm/task/open")(app_ops_task.bhm_task_open)
    app.post("/bhm/task/close")(app_ops_task.bhm_task_close)
    app.get("/bhm/task")(app_ops_task.bhm_task_get)
    app.get("/bhm/tasks")(app_ops_task.bhm_task_list)
    app.post("/bhm/task-context")(app_ops_task.bhm_task_context_update)
    app.get("/bhm/task-context")(app_ops_task.bhm_task_context_get)
    app.post("/bhm/risk-register")(app_ops_task.bhm_risk_register_update)
    app.get("/bhm/risk-register")(app_ops_task.bhm_risk_register_get)
    app.post("/bhm/validation-snapshot")(app_ops_task.bhm_validation_snapshot_save)
    app.get("/bhm/validation-snapshot")(app_ops_task.bhm_validation_snapshot_get)
    app.get("/bhm/query-suggestions")(app_ops_task.bhm_query_suggestions)
    app.post("/bhm/integrity-audit")(app_ops_task.bhm_integrity_audit)
    app.get("/bhm/audit")(app_ops_task.bhm_audit)
    app.post("/bhm/artifact-integrity-audit")(app_ops_task.bhm_artifact_integrity_audit)
    app.post("/bhm/repair-live-indexes")(app_ops_task.bhm_repair_live_indexes)
    app.post("/bhm/entity-extract")(app_ops_task.bhm_entity_extract)
    app.post("/bhm/relation-suggest")(bhm_relation_suggest)
    app.post("/bhm/link-graph-stats")(app_ops_task.bhm_link_graph_stats)
    app.post("/bhm/reindex-memory-metadata")(app_ops_task.bhm_reindex_memory_metadata)
    app.post("/bhm/artifact/restore")(app_ops_task.bhm_artifact_restore)
    app.post("/bhm/artifact/relink")(app_ops_task.bhm_orphan_artifact_relink)
    app.post("/bhm/relation/apply-suggestions")(bhm_relation_apply_suggestions)
    app.get("/bhm/ontology/quarantine")(app_ops_task.bhm_ontology_quarantine_list)
    app.post("/bhm/schema/upgrade-all")(app_ops_task.bhm_schema_upgrade_all)
    app.post("/bhm/agent-activity-rollup")(app_ops_task.bhm_agent_activity_rollup)
    app.post("/bhm/relation/confidence")(bhm_relation_confidence_set)
    app.post("/bhm/relation/vote-quality")(bhm_relation_vote_quality)
    app.post("/bhm/entity-catalog/get")(app_ops_entity.bhm_entity_catalog_get)
    app.post("/bhm/entity-catalog/rebuild")(app_ops_entity.bhm_entity_catalog_rebuild)
    app.post("/bhm/recent-failures-feed")(app_ops_task.bhm_recent_failures_feed)
    app.post("/bhm/artifact/delete")(app_ops_task.bhm_artifact_delete)
    app.post("/bhm/artifact/list-by-type")(app_ops_task.bhm_artifact_list_by_type)
    app.post("/bhm/artifact/usage-stats")(app_ops_task.bhm_artifact_usage_stats)
    app.post("/bhm/link/cycle-detect")(app_ops_task.bhm_link_cycle_detect)
    app.post("/bhm/link/orphan-scan")(bhm_link_orphan_scan)
    app.post("/bhm/validation/trend-report")(app_ops_task.bhm_validation_trend_report)
    app.post("/bhm/entity/search")(app_ops_task.bhm_entity_search)
    app.post("/bhm/entity/link-memories")(bhm_entity_link_memories)
    app.post("/bhm/alias/stats")(app_ops_task.bhm_alias_stats)
    app.post("/bhm/relation/prune-low-quality")(bhm_relation_prune_low_quality)
    app.post("/bhm/review-queue/apply")(bhm_review_queue_apply)
    app.post("/bhm/triage-queue/apply")(bhm_triage_queue_apply)
    app.post("/bhm/artifact/batch-delete")(app_ops_task.bhm_artifact_batch_delete)
    app.post("/bhm/artifact/batch-relink")(app_ops_task.bhm_artifact_batch_relink)
    app.post("/bhm/artifact/batch-restore")(app_ops_task.bhm_artifact_batch_restore)
    app.post("/bhm/schema/validate-strict")(app_ops_task.bhm_schema_validate_strict)
    app.post("/bhm/integrity/repair-strict")(app_ops_task.bhm_integrity_repair_strict)
    app.post("/bhm/admin/export")(app_ops_task.bhm_admin_export)
    app.post("/bhm/admin/import-preview")(app_ops_admin.bhm_admin_import_preview)
    app.post("/bhm/admin/import-apply")(app_ops_admin.bhm_admin_import_apply)
    app.get("/bhm/admin/task-ingress/status")(app_ops_task.bhm_admin_task_ingress_status)
    app.post("/bhm/admin/task-ingress/freeze")(app_ops_task.bhm_admin_task_ingress_freeze)
    app.post("/bhm/admin/task-ingress/resume")(app_ops_task.bhm_admin_task_ingress_resume)
    app.get("/bhm/policy/profile")(app_ops_task.bhm_policy_profile_get)
    app.post("/bhm/policy/profile")(app_ops_task.bhm_policy_profile_set)
    app.post("/bhm/policy/enforce-memory")(bhm_policy_enforce_memory)
    app.post("/bhm/overlap/report")(app_ops_task.bhm_overlap_report)
    app.post("/bhm/overlap/cleanup-apply")(app_ops_task.bhm_overlap_cleanup_apply)
    app.post("/bhm/policy-guard")(app_ops_task.bhm_policy_guard)
    app.post("/bhm/reflect")(app_ops_task.bhm_reflect)
    app.post("/bhm/verify")(app_ops_task.bhm_verify)
    app.post("/bhm/remember")(app_ops_task.bhm_remember)
    app.get("/bhm/observations/store/status")(app_ops_task.bhm_observation_store_status)
    app.get("/bhm/retention/status")(app_ops_task.bhm_retention_status)
