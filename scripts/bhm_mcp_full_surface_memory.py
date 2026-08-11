"""Local, fixture-scoped conformance phase for BHM MCP tools 1-162.

The phase deliberately talks only through the supplied runner.  It does not
touch the Jmaka checkout; its paths are used as read-only source references.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


PROJECT_DEFAULT = "jmaka-function-audit"
JMAKA_ROOT_DEFAULT = r"E:\GitHub\repos\Jmaka"
SOURCE_REFS_DEFAULT = (
    r"E:\GitHub\repos\Jmaka\README.md",
    r"E:\GitHub\repos\Jmaka\Jmaka.slnx",
    r"E:\GitHub\repos\Jmaka\src\Jmaka.Api\appsettings.json",
)


# This is the authoritative numeric order of the memory surface (the first
# repository/indexing tool is 163 and is intentionally covered by another
# phase).
TOOL_ORDER = tuple(
    """
bhm_health bhm_health_slo bhm_diagnostics bhm_preflight bhm_search bhm_context_compile bhm_explain_retrieval bhm_memory_used bhm_get_memory bhm_list_memories bhm_update_memory bhm_archive_memory bhm_forget_preview bhm_forget_apply bhm_search_advanced bhm_recent_activity bhm_upsert_memory bhm_get_memory_links bhm_link_memories bhm_unlink_memories bhm_crystallize bhm_checkpoint_create bhm_checkpoint_list bhm_checkpoint_get_latest bhm_project_map_get bhm_project_resolve bhm_project_retire bhm_project_map_upsert bhm_merge_memories bhm_detect_duplicates bhm_detect_conflicts bhm_memory_lint bhm_delete_memory bhm_get_memories_by_concept bhm_get_memories_by_type bhm_set_memory_confidence bhm_pin_memory bhm_vote_memory_quality bhm_unpin_memory bhm_list_pinned_memories bhm_adr_create bhm_adr_list bhm_adr_supersede bhm_handoff_create bhm_handoff_list bhm_session_record_create bhm_session_record_list bhm_task_open bhm_task_close bhm_task_get bhm_task_list bhm_task_context_update bhm_task_context_get bhm_risk_register_update bhm_risk_register_get bhm_validation_snapshot_save bhm_validation_snapshot_get bhm_source_refs_attach bhm_source_refs_get bhm_memory_timeline bhm_query_suggestions bhm_delete_memory_hard bhm_source_refs_detach bhm_source_refs_replace bhm_memory_restore_from_archive bhm_batch_upsert bhm_batch_link bhm_batch_upsert_memories bhm_batch_attach_source_refs bhm_batch_archive_memories bhm_batch_delete_memories bhm_batch_link_memories bhm_batch_unlink_memories bhm_repair_live_indexes bhm_integrity_audit bhm_memory_diff bhm_rebuild_project_summary bhm_project_summary_get bhm_project_summary_pin bhm_project_summary_list bhm_artifact_integrity_audit bhm_entity_extract bhm_relation_suggest bhm_memory_compact bhm_link_graph_stats bhm_reindex_memory_metadata bhm_memory_schema_validate bhm_memory_type_migrate bhm_search_hybrid bhm_search_by_source_ref bhm_search_by_upsert_key bhm_list_archived_memories bhm_memory_restore_batch bhm_artifact_restore bhm_orphan_artifact_relink bhm_memory_staleness_report bhm_memory_review_queue bhm_memory_triage_queue bhm_project_summary_refresh_all bhm_relation_apply_suggestions bhm_memory_merge_preview bhm_schema_upgrade_all bhm_memory_redact bhm_secret_scan_existing_memories bhm_agent_activity_rollup bhm_project_memory_heatmap bhm_relation_confidence_set bhm_relation_vote_quality bhm_memory_alias_add bhm_memory_alias_remove bhm_alias_resolve bhm_entity_catalog_get bhm_entity_catalog_rebuild bhm_project_summary_compare bhm_memory_usage_stats bhm_recent_failures_feed bhm_memory_restore_hard_deleted_preview bhm_artifact_delete bhm_artifact_list_by_type bhm_artifact_usage_stats bhm_memory_gc_candidates bhm_memory_compaction_report bhm_link_cycle_detect bhm_link_orphan_scan bhm_project_map_compare bhm_validation_trend_report bhm_entity_search bhm_entity_link_memories bhm_alias_stats bhm_relation_prune_low_quality bhm_project_similarity_report bhm_memory_changelog bhm_review_queue_apply bhm_triage_queue_apply bhm_artifact_batch_delete bhm_artifact_batch_relink bhm_artifact_batch_restore bhm_schema_validate_strict bhm_integrity_repair_strict bhm_memory_normalize_metadata bhm_admin_export bhm_admin_import_preview bhm_admin_import_apply bhm_policy_profile_get bhm_policy_profile_set bhm_policy_enforce_memory bhm_overlap_report bhm_overlap_cleanup_apply bhm_policy_guard bhm_remember bhm_profile bhm_insights bhm_lessons_create bhm_lessons_search bhm_observe bhm_slot_list bhm_slot_get bhm_slot_set bhm_slot_append bhm_slot_replace bhm_slot_delete bhm_slot_reflect
""".split()
)


# Required/interesting argument names.  Optional arguments are supplied too;
# BHM tools accept their documented defaults and this makes the runner receipt
# self-describing while remaining tolerant of response-shape differences.
ARGUMENTS = {
    "bhm_health_slo": "max_hook_queue_pending max_hook_queue_failed max_hook_queue_oldest_age_ms max_projection_pending max_projection_failed require_provider_ready",
    "bhm_preflight": "project query limit",
    "bhm_search": "query project limit domain semantic_type priority include_archived include_logs",
    "bhm_context_compile": "query project profile token_budget limit domain semantic_type priority include_archived include_logs",
    "bhm_explain_retrieval": "query project limit memory_type concepts_csv files_csv domain semantic_type priority include_archived include_logs",
    "bhm_memory_used": "ids_csv project reason",
    "bhm_get_memory": "id project",
    "bhm_list_memories": "project memory_type include_archived limit offset",
    "bhm_update_memory": "id project memory_type content concepts_csv files_csv metadata_patch_json",
    "bhm_archive_memory": "id project reason",
    "bhm_forget_preview": "memory_ids_csv upsert_keys_csv project operation reason undo_window_seconds limit",
    "bhm_forget_apply": "preview_digest memory_ids_csv upsert_keys_csv project operation reason undo_window_seconds limit confirm",
    "bhm_search_advanced": "query project memory_type concepts_csv files_csv include_archived include_logs domain semantic_type priority limit offset",
    "bhm_recent_activity": "project memory_type include_archived limit",
    "bhm_upsert_memory": "upsert_key content project memory_type concepts_csv files_csv metadata_json",
    "bhm_get_memory_links": "id project",
    "bhm_link_memories": "source_id target_id relation project metadata_json",
    "bhm_unlink_memories": "source_id target_id relation project",
    "bhm_crystallize": "source_ids_csv project title summary target_type concepts_csv files_csv upsert_key",
    "bhm_checkpoint_create": "project checkpoint_type title content done next checks risks concepts_csv files_csv upsert_key",
    "bhm_checkpoint_list": "project checkpoint_type limit offset",
    "bhm_checkpoint_get_latest": "project checkpoint_type",
    "bhm_project_map_get": "project",
    "bhm_project_resolve": "project",
    "bhm_project_retire": "project apply capability backup_dir",
    "bhm_project_map_upsert": "project title auth routing tests deploy i18n websocket risks notes files_csv concepts_csv upsert_key",
    "bhm_merge_memories": "source_id target_id project archive_source",
    "bhm_detect_duplicates": "project limit include_archived",
    "bhm_detect_conflicts": "project limit include_archived",
    "bhm_memory_lint": "id project",
    "bhm_delete_memory": "id project",
    "bhm_get_memories_by_concept": "concept project limit offset",
    "bhm_get_memories_by_type": "memory_type project limit offset",
    "bhm_set_memory_confidence": "id confidence project",
    "bhm_pin_memory": "id project",
    "bhm_vote_memory_quality": "id vote project voter",
    "bhm_unpin_memory": "id project",
    "bhm_list_pinned_memories": "project limit offset",
    "bhm_adr_create": "project title context decision consequences status files_csv concepts_csv upsert_key",
    "bhm_adr_list": "project limit offset",
    "bhm_adr_supersede": "project old_id new_id",
    "bhm_handoff_create": "project title current_state decisions validation next_agent_action next_owner_id handoff_sla_deadline files_csv concepts_csv upsert_key",
    "bhm_handoff_list": "project limit offset",
    "bhm_session_record_create": "project title done next checks risks decisions files_touched_csv conversation_notes transcript_ref upsert_key",
    "bhm_session_record_list": "project limit offset",
    "bhm_task_open": "task_id intent project title scope_in_csv scope_out_csv repo owner session_id correlation_id files_touched_csv metadata_json upsert_key",
    "bhm_task_close": "task_id project done next_step checks risks decisions validation files_touched_csv conversation_notes transcript_ref metadata_json",
    "bhm_task_get": "task_id project",
    "bhm_task_list": "project status limit offset",
    "bhm_task_context_update": "project title current_task status pending_items guidance next_step files_touched_csv upsert_key",
    "bhm_task_context_get": "project",
    "bhm_risk_register_update": "project title summary top_risks_csv mitigations_csv owner upsert_key",
    "bhm_risk_register_get": "project",
    "bhm_validation_snapshot_save": "project title lint tests smoke docs overall_status command_summary upsert_key",
    "bhm_validation_snapshot_get": "project",
    "bhm_source_refs_attach": "id refs_csv project",
    "bhm_source_refs_get": "id project",
    "bhm_memory_timeline": "project concept memory_type include_archived limit",
    "bhm_query_suggestions": "project",
    "bhm_delete_memory_hard": "id project",
    "bhm_source_refs_detach": "id refs_csv project",
    "bhm_source_refs_replace": "id refs_csv project",
    "bhm_memory_restore_from_archive": "id project",
    "bhm_batch_upsert": "items project",
    "bhm_batch_link": "items project",
    "bhm_batch_upsert_memories": "items_json project",
    "bhm_batch_attach_source_refs": "items_json project",
    "bhm_batch_archive_memories": "items_json project",
    "bhm_batch_delete_memories": "items_json project",
    "bhm_batch_link_memories": "items_json project",
    "bhm_batch_unlink_memories": "items_json project",
    "bhm_repair_live_indexes": "project aggregate remove_orphan_links remove_orphan_artifacts",
    "bhm_integrity_audit": "project",
    "bhm_memory_diff": "left_id right_id project",
    "bhm_rebuild_project_summary": "project upsert_key",
    "bhm_project_summary_get": "project",
    "bhm_project_summary_pin": "project",
    "bhm_project_summary_list": "project limit offset",
    "bhm_artifact_integrity_audit": "project",
    "bhm_entity_extract": "id project",
    "bhm_relation_suggest": "project limit",
    "bhm_memory_compact": "id summary project",
    "bhm_link_graph_stats": "project",
    "bhm_reindex_memory_metadata": "project aggregate",
    "bhm_memory_schema_validate": "id project",
    "bhm_memory_type_migrate": "id new_type project",
    "bhm_search_hybrid": "query project domain semantic_type priority include_archived include_logs limit",
    "bhm_search_by_source_ref": "ref project limit",
    "bhm_search_by_upsert_key": "upsert_key project",
    "bhm_list_archived_memories": "project limit offset",
    "bhm_memory_restore_batch": "items_json",
    "bhm_artifact_restore": "artifact_type artifact_id project",
    "bhm_orphan_artifact_relink": "artifact_type artifact_id target_memory_id project",
    "bhm_memory_staleness_report": "project days limit",
    "bhm_memory_review_queue": "project limit include_conflicts include_closed",
    "bhm_memory_triage_queue": "project limit include_closed",
    "bhm_project_summary_refresh_all": "projects_csv project aggregate",
    "bhm_relation_apply_suggestions": "project aggregate min_score limit include_relates_to",
    "bhm_memory_merge_preview": "project source_id target_id",
    "bhm_schema_upgrade_all": "project aggregate",
    "bhm_memory_redact": "id project patterns_csv replacement",
    "bhm_secret_scan_existing_memories": "project limit",
    "bhm_agent_activity_rollup": "project",
    "bhm_project_memory_heatmap": "project",
    "bhm_relation_confidence_set": "source_id target_id relation project confidence",
    "bhm_relation_vote_quality": "source_id target_id relation project vote voter",
    "bhm_memory_alias_add": "id alias project",
    "bhm_memory_alias_remove": "id alias project",
    "bhm_alias_resolve": "alias project",
    "bhm_entity_catalog_get": "project",
    "bhm_entity_catalog_rebuild": "project aggregate",
    "bhm_project_summary_compare": "left_project right_project",
    "bhm_memory_usage_stats": "project",
    "bhm_recent_failures_feed": "project limit",
    "bhm_memory_restore_hard_deleted_preview": "id project",
    "bhm_artifact_delete": "artifact_type artifact_id project delete_backing_memory",
    "bhm_artifact_list_by_type": "artifact_type project limit offset",
    "bhm_artifact_usage_stats": "project",
    "bhm_memory_gc_candidates": "project stale_days limit",
    "bhm_memory_compaction_report": "project min_chars min_lines limit",
    "bhm_link_cycle_detect": "project limit",
    "bhm_link_orphan_scan": "project",
    "bhm_project_map_compare": "left_project right_project",
    "bhm_validation_trend_report": "project limit",
    "bhm_entity_search": "query project limit",
    "bhm_entity_link_memories": "entity project relation limit",
    "bhm_alias_stats": "project",
    "bhm_relation_prune_low_quality": "project aggregate max_confidence max_quality_score remove_unscored",
    "bhm_project_similarity_report": "project limit",
    "bhm_memory_changelog": "id project limit",
    "bhm_review_queue_apply": "project limit mark_needs_review auto_redact_secrets queue_ids_csv status",
    "bhm_triage_queue_apply": "project limit min_score include_relates_to",
    "bhm_artifact_batch_delete": "artifact_type artifact_ids_csv project delete_backing_memory",
    "bhm_artifact_batch_relink": "artifact_type items_json project",
    "bhm_artifact_batch_restore": "artifact_type artifact_ids_csv project",
    "bhm_schema_validate_strict": "project include_archived",
    "bhm_integrity_repair_strict": "project remove_orphan_links remove_orphan_artifacts normalize_metadata",
    "bhm_memory_normalize_metadata": "project",
    "bhm_admin_export": "project include_archived include_artifacts export_name",
    "bhm_admin_import_preview": "path project",
    "bhm_admin_import_apply": "path merge_mode project",
    "bhm_policy_profile_set": "max_content_chars max_lines require_project require_memory_type block_secret_like block_raw_logs",
    "bhm_policy_enforce_memory": "id project auto_redact",
    "bhm_overlap_report": "project limit",
    "bhm_overlap_cleanup_apply": "project aggregate limit archive_sources",
    "bhm_policy_guard": "content project memory_type",
    "bhm_remember": "content project memory_type concepts files metadata",
    "bhm_profile": "project",
    "bhm_insights": "project limit",
    "bhm_lessons_create": "content project context confidence tags_csv",
    "bhm_lessons_search": "query project limit",
    "bhm_observe": "hook_type session_id cwd project data_json timestamp",
    "bhm_slot_list": "project",
    "bhm_slot_get": "label project",
    "bhm_slot_set": "label content project size_limit description pinned scope",
    "bhm_slot_append": "label text project",
    "bhm_slot_replace": "label content project",
    "bhm_slot_delete": "label project",
    "bhm_slot_reflect": "label project",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _walk(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _ids(value: Any) -> list[str]:
    found: list[str] = []
    for obj in _walk(value):
        for key in (
            "id",
            "memory_id",
            "source_id",
            "target_id",
            "artifact_id",
            "task_id",
        ):
            item = obj.get(key)
            if isinstance(item, (str, int)) and str(item) not in found:
                found.append(str(item))
    return found


def _first(state: dict[str, Any], key: str = "primary_id") -> str:
    ids = state.get("fixture_ids", {})
    return str(ids.get(key) or ids.get("primary_id") or "fixture-missing")


def _value(arg: str, name: str, state: dict[str, Any]) -> Any:
    project = state["project"]
    ids = state.get("fixture_ids", {})
    if arg == "project":
        return project
    if arg == "left_project":
        return project
    if arg == "right_project":
        return state.get("peer_project", project)
    if arg in {"query", "semantic_query"}:
        return "Jmaka"
    if arg == "profile":
        return "standard"
    if arg == "root" or arg == "cwd":
        return state["repo_root"]
    if arg in {"id", "left_id", "right_id", "target_memory_id"}:
        return _first(state, "primary_id")
    if arg == "source_id":
        return _first(state, "link_source_id")
    if arg == "target_id":
        return _first(state, "link_target_id")
    if arg == "memory_ids_csv" or arg == "ids_csv":
        return ",".join(ids.values()) or "fixture-missing"
    if arg == "upsert_keys_csv":
        return "jmaka-audit-primary"
    if arg == "upsert_key":
        return f"{project}:{name}:fixture"
    if arg == "refs_csv" or arg == "files_csv":
        return ",".join(state["source_refs"])
    if arg == "files_touched_csv" or arg in {"scope_in_csv", "scope_out_csv"}:
        return ",".join(state["source_refs"])
    if arg == "content" or arg == "summary":
        return "Jmaka validation fixture; local-only and disposable."
    if arg in {"title", "description"}:
        return f"Jmaka validation {name}"
    if arg in {
        "context",
        "decision",
        "consequences",
        "current_state",
        "decisions",
        "validation",
        "next_agent_action",
        "done",
        "next",
        "next_step",
        "checks",
        "risks",
        "guidance",
        "pending_items",
        "command_summary",
        "notes",
        "summary",
    }:
        return "Fixture-only validation data; no repository mutation."
    if arg in {"metadata_json", "metadata_patch_json", "data_json"}:
        return _json({"fixture": True, "source": "Jmaka"})
    if arg == "items_json":
        return _json([])
    if arg == "items":
        return []
    if arg == "operation":
        return "tombstone"
    if arg == "reason":
        return "jmaka-mcp-186 conformance fixture"
    if arg == "memory_type" or arg == "new_type" or arg == "target_type":
        return "fact"
    if arg == "domain":
        return "general"
    if arg == "semantic_type":
        return "knowledge"
    if arg == "priority":
        return "medium"
    if arg in {
        "include_archived",
        "include_logs",
        "include_conflicts",
        "include_closed",
        "aggregate",
        "remove_orphan_links",
        "remove_orphan_artifacts",
        "normalize_metadata",
        "archive_source",
        "archive_sources",
        "apply",
        "confirm",
        "require_provider_ready",
        "require_project",
        "require_memory_type",
        "block_secret_like",
        "block_raw_logs",
        "auto_redact",
        "mark_needs_review",
        "auto_redact_secrets",
        "include_relates_to",
        "include_artifacts",
        "delete_backing_memory",
        "remove_unscored",
        "pinned",
        "defer_graph",
        "graph_only",
        "force_refresh",
        "build_graph",
        "semantic_fusion",
        "include_git_history",
    }:
        return False
    if arg in {
        "limit",
        "offset",
        "days",
        "stale_days",
        "min_chars",
        "min_lines",
        "size_limit",
        "max_files_per_run",
        "cycles",
        "interval_seconds",
        "depth",
        "context",
        "line",
        "limit",
    }:
        return 10
    if arg == "token_budget":
        return 1200
    if arg in {
        "confidence",
        "min_score",
        "max_confidence",
        "max_quality_score",
        "semantic_weight",
        "semantic_min_score",
    }:
        return 0.5
    if arg in {"vote", "quality_score"}:
        return 3
    if arg in {
        "undo_window_seconds",
        "time_budget_ms",
        "max_tokens",
        "max_content_chars",
        "max_lines",
        "max_hook_queue_pending",
        "max_hook_queue_failed",
        "max_hook_queue_oldest_age_ms",
        "max_projection_pending",
        "max_projection_failed",
    }:
        return 1000
    if arg == "voter" or arg == "owner" or arg == "next_owner_id":
        return "jmaka-audit-runner"
    if arg == "task_id":
        return state.setdefault("task_id", f"jmaka-task-{state.get('run_id', 'local')}")
    if arg == "label":
        return state.setdefault("slot_label", "jmaka-validation-slot")
    if arg == "alias":
        return state.setdefault("alias", "jmaka-validation-alias")
    if arg == "session_id":
        return state.setdefault(
            "session_id", f"jmaka-session-{state.get('run_id', 'local')}"
        )
    if arg == "status":
        return "open"
    if arg == "scope":
        return "project"
    if arg == "timestamp" or arg == "handoff_sla_deadline":
        return "2026-08-10T19:30:00Z"
    if arg == "path":
        return state.get("export_path", "")
    if arg == "hook_type":
        return "codex_stop"
    if arg == "artifact_type":
        return "checkpoint"
    if arg in {
        "alias",
        "label",
        "entity",
        "session_id",
        "task_id",
        "intent",
        "repo",
        "profile",
        "replacement",
        "new_type",
        "scope",
        "export_name",
        "merge_mode",
        "capability",
        "transcript_ref",
        "correlation_id",
        "timestamp",
        "snapshot_id",
        "expected_job_id",
        "expected_state_digest",
        "artifact_id",
        "artifact_ids_csv",
        "detached_signature_b64",
        "detached_public_key_b64",
        "adoption_receipt_digest",
        "rollback_anchor_snapshot_id",
        "rollback_anchor_digest",
        "changed_paths",
        "base_revision",
        "expected_graph_digest",
        "relation",
        "upsert_key",
    }:
        return ids.get("task_id", f"jmaka-{name}-fixture")
    if arg == "preview_digest":
        return state.get("forget_preview_digest", "fixture-preview")
    if arg == "backup_dir":
        return state.get("backup_dir", "")
    if arg == "projects_csv":
        return project
    if (
        arg == "concepts_csv"
        or arg == "top_risks_csv"
        or arg == "mitigations_csv"
        or arg == "tags_csv"
    ):
        return "Jmaka,validation"
    if arg == "text":
        return "fixture append"
    return "fixture"


_SEMANTICALLY_VALIDATED = {
    "bhm_forget_preview",
    "bhm_forget_apply",
    "bhm_upsert_memory",
    "bhm_batch_upsert",
    "bhm_review_queue_apply",
    "bhm_slot_reflect",
}


def _validate_tool_response(name: str, value: Any) -> tuple[bool, str]:
    payload = value if isinstance(value, Mapping) else {}
    if name == "bhm_forget_preview":
        digest = str(payload.get("plan_digest") or "")
        ok = payload.get("candidate_count") == 1 and len(digest) == 64
        return ok, "one forget candidate with canonical plan digest"
    if name == "bhm_forget_apply":
        return payload.get("count") == 1, "exactly one forget candidate applied"
    if name == "bhm_upsert_memory":
        memory = (
            payload.get("memory") if isinstance(payload.get("memory"), Mapping) else {}
        )
        ok = payload.get("success") is True and bool(memory.get("id"))
        return ok, "upsert returned a real memory id"
    if name == "bhm_batch_upsert":
        ok = payload.get("committed") is True and int(payload.get("count") or 0) >= 11
        return ok, "atomic fixture batch committed"
    if name == "bhm_review_queue_apply":
        ok = payload.get("count") == 1 and not (payload.get("missing_queue_ids") or [])
        return ok, "one real review queue item applied"
    if name == "bhm_slot_reflect":
        slot = payload.get("slot") if isinstance(payload.get("slot"), Mapping) else {}
        ok = (
            payload.get("success") is True
            and slot.get("label") == "jmaka-validation-slot"
        )
        return ok, "live validation slot reflected"
    return True, "call_completed"


async def _call(
    runner: Any,
    state: dict[str, Any],
    name: str,
    number: int,
    args: dict[str, Any],
    phase: str,
    *,
    expected_empty: bool = False,
    expected_rejection: bool = False,
    note: str = "",
) -> Any:
    try:
        result = await runner.call(
            number,
            name,
            args,
            expected_empty=expected_empty,
            expected_rejection=expected_rejection,
            note=note or phase,
            stage=phase,
            validator=(lambda value: _validate_tool_response(name, value))
            if name in _SEMANTICALLY_VALIDATED
            else None,
        )
    except (
        Exception
    ) as exc:  # runner normally records errors; preserve receipt if it raises.
        state.setdefault("harness_errors", []).append(
            {"tool": name, "number": number, "error": str(exc)}
        )
        return None
    state.setdefault("outputs", {})[name] = result
    state.setdefault("called_numbers", []).append(number)
    ids = _ids(result)
    if ids:
        state.setdefault("observed_ids", {}).setdefault(name, []).extend(ids)
    if name == "bhm_forget_preview":
        for obj in _walk(result):
            digest = (
                obj.get("preview_digest") or obj.get("plan_digest") or obj.get("digest")
            )
            if digest:
                state["forget_preview_digest"] = str(digest)
                break
    if name == "bhm_admin_export":
        for obj in _walk(result):
            for key in ("path", "export_path", "snapshot_path"):
                if obj.get(key):
                    state["export_path"] = str(obj[key])
                    return result
    if name == "bhm_task_open":
        for obj in _walk(result):
            if obj.get("task_id"):
                state["task_key"] = str(obj["task_id"])
                break
    if name == "bhm_adr_create" and not state.get("adr_old_id"):
        for obj in _walk(result):
            if obj.get("id", "").startswith("adr_"):
                state["adr_old_id"] = str(obj["id"])
                break
    return result


def _policy(value: Any) -> dict[str, Any]:
    keys = (
        "max_content_chars",
        "max_lines",
        "require_project",
        "require_memory_type",
        "block_secret_like",
        "block_raw_logs",
    )
    for obj in _walk(value):
        if any(key in obj for key in keys):
            return {key: obj[key] for key in keys if key in obj}
    return {}


def _special_args(
    name: str, args: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    project = state["project"]
    refs_csv = ",".join(state["source_refs"])

    id_by_tool = {
        "bhm_archive_memory": "archive_id",
        "bhm_memory_restore_from_archive": "archive_id",
        "bhm_delete_memory": "delete_id",
        "bhm_delete_memory_hard": "hard_delete_id",
        "bhm_memory_restore_hard_deleted_preview": "hard_delete_id",
    }
    if name in id_by_tool:
        args["id"] = _first(state, id_by_tool[name])

    if name == "bhm_project_retire":
        args.update(
            {"project": project, "apply": False, "capability": "", "backup_dir": ""}
        )
    elif name == "bhm_forget_preview":
        args.update(
            {"memory_ids_csv": _first(state, "forget_id"), "operation": "tombstone"}
        )
        args.pop("upsert_keys_csv", None)
    elif name == "bhm_forget_apply":
        args.update(
            {
                "memory_ids_csv": _first(state, "forget_id"),
                "preview_digest": state.get("forget_preview_digest", "fixture-preview"),
                "operation": "tombstone",
                "confirm": True,
            }
        )
        args.pop("upsert_keys_csv", None)
    elif name in {
        "bhm_link_memories",
        "bhm_unlink_memories",
        "bhm_relation_confidence_set",
        "bhm_relation_vote_quality",
    }:
        args.update(
            {
                "source_id": _first(state, "link_source_id"),
                "target_id": _first(state, "link_target_id"),
                "relation": "relates_to",
            }
        )
    elif name in {"bhm_merge_memories", "bhm_memory_merge_preview"}:
        args.update(
            {
                "source_id": _first(state, "merge_source_id"),
                "target_id": _first(state, "merge_target_id"),
            }
        )
    elif name == "bhm_crystallize":
        args["source_ids_csv"] = ",".join(
            (_first(state, "link_source_id"), _first(state, "link_target_id"))
        )
    elif name == "bhm_adr_supersede":
        adr_ids = state.get("observed_ids", {}).get("bhm_adr_create", [])
        args.update(
            {
                "old_id": state.get(
                    "adr_old_id", adr_ids[0] if adr_ids else _first(state)
                ),
                "new_id": state.get(
                    "adr_new_id",
                    adr_ids[-1]
                    if len(adr_ids) > 1
                    else _first(state, "link_target_id"),
                ),
            }
        )
    elif name == "bhm_source_refs_replace":
        args["refs_csv"] = refs_csv
    elif name == "bhm_source_refs_detach":
        args["refs_csv"] = state["source_refs"][-1]
    elif name == "bhm_batch_link":
        args["items"] = [
            {
                "source_id": _first(state, "link_source_id"),
                "target_id": _first(state, "link_target_id"),
                "relation": "relates_to",
                "project": project,
            }
        ]
    elif name in {"bhm_batch_link_memories", "bhm_batch_unlink_memories"}:
        args["items_json"] = _json(
            [
                {
                    "source_id": _first(state, "link_source_id"),
                    "target_id": _first(state, "link_target_id"),
                    "relation": "relates_to",
                    "project": project,
                }
            ]
        )
    elif name == "bhm_batch_attach_source_refs":
        args["items_json"] = _json(
            [{"id": _first(state), "refs": state["source_refs"], "project": project}]
        )
    elif name == "bhm_batch_archive_memories":
        args["items_json"] = _json(
            [{"id": _first(state, "batch_archive_id"), "project": project}]
        )
    elif name == "bhm_batch_delete_memories":
        args["items_json"] = _json(
            [{"id": _first(state, "batch_delete_id"), "project": project}]
        )
    elif name == "bhm_memory_restore_batch":
        args["items_json"] = _json(
            [{"id": _first(state, "batch_archive_id"), "project": project}]
        )
    elif name == "bhm_admin_import_apply":
        args.update(
            {
                "path": state.get("export_path", ""),
                "merge_mode": "upsert",
                "project": project,
            }
        )
    elif name == "bhm_admin_import_preview":
        args.update({"path": state.get("export_path", ""), "project": project})
    elif name == "bhm_task_close":
        args["task_id"] = state.get("task_key", args.get("task_id"))
    elif name == "bhm_task_get":
        args["task_id"] = state.get("task_key", args.get("task_id"))
    elif name == "bhm_review_queue_apply":
        args.update(
            {
                "queue_ids_csv": state.get("review_queue_id", "fixture-missing"),
                "mark_needs_review": True,
                "status": "needs_review",
            }
        )
    elif name == "bhm_observe":
        args.update(
            {
                "hook_type": "codex_stop",
                "session_id": state.get("session_id", "jmaka-audit-session"),
                "timestamp": "2026-08-10T19:30:00Z",
            }
        )
    elif name == "bhm_remember":
        args.update(
            {
                "content": "Jmaka remember fixture; local-only and disposable.",
                "project": project,
                "memory_type": "fact",
                "concepts": ["Jmaka", "validation"],
                "files": list(state["source_refs"]),
                "metadata": {"fixture": True},
            }
        )
    elif name == "bhm_policy_profile_set":
        args = dict(state.get("policy_before", {}))
    elif name in {
        "bhm_artifact_restore",
        "bhm_artifact_delete",
        "bhm_artifact_batch_delete",
        "bhm_artifact_batch_relink",
        "bhm_artifact_batch_restore",
        "bhm_orphan_artifact_relink",
    }:
        artifact_ids = state.get("artifact_ids", [])
        artifact_id = artifact_ids[0] if artifact_ids else f"{project}-missing-artifact"
        if "artifact_type" in args:
            args["artifact_type"] = "checkpoint"
        if "artifact_id" in args:
            args["artifact_id"] = artifact_id
        if "artifact_ids_csv" in args:
            args["artifact_ids_csv"] = artifact_id
        if "delete_backing_memory" in args:
            args["delete_backing_memory"] = False
        if name == "bhm_artifact_batch_relink":
            args["items_json"] = _json(
                [{"artifact_id": artifact_id, "target_memory_id": _first(state)}]
            )
    return args


async def run_memory_tools(runner: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Execute every memory tool 2-162 exactly once in the primary receipt set.

    Tool 1 (``bhm_health``) is owned by the main runner.  Calls are deliberately
    reordered where fixtures are prerequisites; their canonical catalog number
    remains attached to every receipt.
    """
    state.setdefault("project", f"{PROJECT_DEFAULT}-{state.get('run_id', 'local')}")
    state.setdefault("peer_project", state["project"])
    state.setdefault("repo_root", JMAKA_ROOT_DEFAULT)
    state.setdefault("source_refs", list(SOURCE_REFS_DEFAULT))
    state.setdefault("fixture_ids", {})
    state.setdefault("observed_ids", {})
    state.setdefault("called_numbers", [])

    # Cheap preflight first.  Tool 1 is already executed by the parent runner.
    for number in (2, 3, 4):
        name = TOOL_ORDER[number - 1]
        args = {
            arg: _value(arg, name, state) for arg in ARGUMENTS.get(name, "").split()
        }
        await _call(runner, state, name, number, args, "preflight")

    # One atomic batch creates every disposable fixture before ID-based tools
    # run. Tool 17 then validates normal upsert idempotently.
    fixture_specs = (
        ("primary_id", "primary"),
        ("link_source_id", "link-source"),
        ("link_target_id", "link-target"),
        ("archive_id", "archive"),
        ("delete_id", "delete"),
        ("hard_delete_id", "hard-delete"),
        ("forget_id", "forget"),
        ("merge_source_id", "merge-source"),
        ("merge_target_id", "merge-target"),
        ("batch_archive_id", "batch-archive"),
        ("batch_delete_id", "batch-delete"),
    )
    batch_items = [
        {
            "upsert_key": f"{state['project']}:{suffix}",
            "content": f"Jmaka {suffix} fixture; local-only and disposable.",
            "memory_type": "fact",
            "concepts": ["Jmaka", "validation"],
            "files": list(state["source_refs"]),
            "metadata": {"fixture": True, "slot": key},
        }
        for key, suffix in fixture_specs
    ]
    batch = await _call(
        runner,
        state,
        "bhm_batch_upsert",
        66,
        {"items": batch_items, "project": state["project"]},
        "fixtures",
    )
    upserted_ids: dict[str, str] = {}
    for obj in _walk(batch):
        candidate = obj.get("upserted_ids")
        if isinstance(candidate, Mapping):
            upserted_ids.update(
                {str(key): str(value) for key, value in candidate.items()}
            )
    for key, suffix in fixture_specs:
        memory_id = upserted_ids.get(f"{state['project']}:{suffix}")
        if memory_id:
            state["fixture_ids"][key] = memory_id

    await _call(
        runner,
        state,
        "bhm_upsert_memory",
        17,
        {
            "upsert_key": f"{state['project']}:primary",
            "content": "Jmaka primary fixture; local-only and disposable.",
            "project": state["project"],
            "memory_type": "fact",
            "concepts_csv": "Jmaka,validation",
            "files_csv": ",".join(state["source_refs"]),
            "metadata_json": _json({"fixture": True, "slot": "primary_id"}),
        },
        "fixtures",
    )

    await _call(
        runner,
        state,
        "bhm_project_map_upsert",
        28,
        {
            "project": state["project"],
            "title": "Jmaka MCP 186 validation",
            "files_csv": ",".join(state["source_refs"]),
            "upsert_key": f"{state['project']}:map",
        },
        "fixtures",
    )
    await runner.call(
        None,
        "bhm_project_map_upsert",
        {
            "project": state["peer_project"],
            "title": "Jmaka MCP 186 peer validation",
            "files_csv": ",".join(state["source_refs"]),
            "upsert_key": f"{state['peer_project']}:map",
        },
        counts_toward_catalog=False,
        stage="prerequisite-peer-map",
        note="create disposable peer project map",
    )
    await runner.call(
        None,
        "bhm_rebuild_project_summary",
        {
            "project": state["peer_project"],
            "upsert_key": f"{state['peer_project']}:project-summary",
        },
        counts_toward_catalog=False,
        stage="prerequisite-peer-summary",
        note="materialize disposable peer project summary",
    )

    policy_before = await _call(
        runner, state, "bhm_policy_profile_get", 144, {}, "policy-capture"
    )
    state["policy_before"] = _policy(policy_before)

    already_called = {2, 3, 4, 17, 28, 66, 144}
    artifact_tools = {
        "bhm_artifact_restore",
        "bhm_orphan_artifact_relink",
        "bhm_artifact_delete",
        "bhm_artifact_batch_delete",
        "bhm_artifact_batch_relink",
        "bhm_artifact_batch_restore",
    }
    for number, name in enumerate(TOOL_ORDER, 1):
        if number == 1 or number in already_called or number == 161:
            continue
        if number == 107:
            await runner.call(
                None,
                "bhm_link_memories",
                {
                    "source_id": _first(state, "link_source_id"),
                    "target_id": _first(state, "link_target_id"),
                    "relation": "relates_to",
                    "project": state["project"],
                },
                counts_toward_catalog=False,
                stage="prerequisite-quality-link",
                note="create a real relation before quality voting",
            )
        args = {
            arg: _value(arg, name, state) for arg in ARGUMENTS.get(name, "").split()
        }
        args = _special_args(name, args, state)
        await _call(
            runner,
            state,
            name,
            number,
            args,
            "memory",
            expected_rejection=name in artifact_tools and not state.get("artifact_ids"),
            note=(
                "fixture artifact may not exist"
                if name in artifact_tools and not state.get("artifact_ids")
                else "memory conformance"
            ),
        )
        if name == "bhm_memory_review_queue":
            for obj in _walk(state.get("outputs", {}).get(name)):
                queue_id = obj.get("queue_id")
                if queue_id:
                    state["review_queue_id"] = str(queue_id)
                    break
        if name == "bhm_adr_create" and not state.get("adr_new_id"):
            # Supersede requires two real ADR records in the disposable project.
            second = await runner.call(
                None,
                "bhm_adr_create",
                {
                    "project": state["project"],
                    "title": "Jmaka validation ADR successor",
                    "context": "Fixture-only validation data; no repository mutation.",
                    "decision": "Fixture-only validation data; no repository mutation.",
                    "consequences": "Fixture-only validation data; no repository mutation.",
                    "status": "accepted",
                    "files_csv": ",".join(state["source_refs"]),
                    "concepts_csv": "Jmaka,validation",
                    "upsert_key": f"{state['project']}:adr-successor",
                },
                counts_toward_catalog=False,
                stage="prerequisite-adr-successor",
                note="create a second disposable ADR for supersede coverage",
            )
            adr_ids = _ids(second)
            if adr_ids:
                state["adr_new_id"] = adr_ids[0]

    # Reflect while the slot exists, then validate deletion last.
    name = "bhm_slot_delete"
    args = {arg: _value(arg, name, state) for arg in ARGUMENTS.get(name, "").split()}
    await _call(runner, state, name, 161, args, "memory")

    expected = set(range(2, 163))
    covered = set(state["called_numbers"])
    state["memory_coverage"] = {
        "expected": 161,
        "covered": len(expected & covered),
        "missing": sorted(expected - covered),
        "duplicates": sorted(
            number for number in expected if state["called_numbers"].count(number) > 1
        ),
    }
    return state
