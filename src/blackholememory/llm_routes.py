"""LLM surface (/bhm/llm/*): capabilities, model router, cache, jobs, delegation and long tasks.

Moved out of app.py (FIX-017). Shared app-level callables are reached through ``_deps`` (late bound,
configured once by app.py), so monkeypatching the app names keeps working.
Sub-operations partitioned into llm_routes_base, llm_routes_preview, llm_routes_jobs.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from . import llm_routes_base
from .documentation_factory import (
    DOCUMENTATION_FACTORY_FEATURES,
    DOCUMENTATION_FACTORY_SCHEMA_VERSION,
)
from .llm_cache import (
    LLM_CACHE_DEFAULT_TTL_SECONDS,
    LLM_CACHE_MAX_ENTRIES,
    LLM_CACHE_POLICY_VERSION,
)
from .llm_candidates import (
    LLM_CANDIDATE_JUDGE_VERSION,
    LLM_CANDIDATE_MAX,
    LLM_CANDIDATE_ROLES,
    LLM_CANDIDATE_SCHEMA_VERSION,
)
from .llm_delegation_policy import (
    LLM_DELEGATION_POLICY_VERSION,
    delegation_policy_snapshot,
)
from .llm_gateway import GATEWAY_SCHEMA_VERSION
from .llm_job_queue import (
    LLM_JOB_QUEUE_SCHEMA_VERSION,
    LLMJobQueueError,
)
from .llm_learning import (
    LLM_LEARNING_MAX_RECORDS,
    LLM_LEARNING_POLICY_VERSION,
)
from .llm_long_tasks import (
    LLM_LONG_TASK_MAX_CHUNKS,
    LLM_LONG_TASK_MAX_FANOUT,
    LLM_LONG_TASK_PLAN_VERSION,
)
from .llm_resource_governor import LLMResourceGovernorError
from .llm_routes_base import (
    _Deps,  # noqa: F401
    _LLM_CACHE_STORE,  # noqa: F401
    _LLM_JOB_QUEUE,  # noqa: F401
    _LLM_JOB_QUEUE_CAPACITY,  # noqa: F401
    _LLM_LEARNING_STORE,  # noqa: F401
    _LLM_LONG_TASK_STORE,  # noqa: F401
    _deps,
    _llm_public_job,  # noqa: F401
    _repository_intelligence_scope,  # noqa: F401
    configure,  # noqa: F401
)
from .llm_routes_jobs import (
    bhm_llm_cancel_job,
    bhm_llm_job_result,
    bhm_llm_job_status,
    bhm_llm_submit_job,
)
from .llm_routes_preview import (
    bhm_llm_cache_preview,
    bhm_llm_cache_status,
    bhm_llm_candidate_plan,
    bhm_llm_code_fabric_plan,
    bhm_llm_delegation_decide,
    bhm_llm_documentation_factory_preview,
    bhm_llm_learning_curate,
    bhm_llm_learning_review,
    bhm_llm_learning_status,
    bhm_llm_memory_foundry_preview,
    bhm_llm_model_router_decide,
    bhm_llm_model_router_snapshot,
    bhm_llm_night_shift_preview,
    bhm_llm_qa_incident_preview,
    bhm_llm_repository_intelligence_preview,
    bhm_llm_retrieval_lab_preview,
)
from .llm_safety import (
    LLM_SAFETY_POLICY_VERSION,
    PROPOSAL_AUTHORITY,
)
from .mcp_surfaces import CORE_TOOL_NAMES
from .memory_foundry import (
    MEMORY_FOUNDRY_SCHEMA_VERSION,
)
from .model_router import (
    MODEL_ROUTER_CAPABILITIES,
    MODEL_ROUTER_CONTEXT_PROFILES,
    MODEL_ROUTER_SCHEMA_VERSION,
)
from .night_shift import (
    NIGHT_SHIFT_SAFE_JOB_TYPES,
    NIGHT_SHIFT_SCHEMA_VERSION,
)
from .observation_security import redact_secret_text
from .qa_incident_factory import (
    QA_INCIDENT_FEATURES,
    QA_INCIDENT_SCHEMA_VERSION,
)
from .repository_intelligence import (
    REPOSITORY_INTELLIGENCE_MAX_FILES,
    REPOSITORY_INTELLIGENCE_SCHEMA_VERSION,
)
from .retrieval_lab import (
    RETRIEVAL_LAB_FEATURES,
    RETRIEVAL_LAB_SCHEMA_VERSION,
)
from .safe_patch_factory import (
    SAFE_PATCH_MAX_DIFF_BYTES,
    SAFE_PATCH_MAX_FILES,
    SAFE_PATCH_MAX_TIMEOUT_SECONDS,
    SAFE_PATCH_SCHEMA_VERSION,
)


def bhm_llm_capabilities() -> dict[str, Any]:
    """Expose the bounded Codex/plugin delegation contract without starting execution."""

    queue_info: dict[str, Any] = {
        "schema_version": LLM_JOB_QUEUE_SCHEMA_VERSION,
        "path": str(llm_routes_base._LLM_JOB_QUEUE.path),
        "capacity": llm_routes_base._LLM_JOB_QUEUE.capacity,
        "exists": llm_routes_base._LLM_JOB_QUEUE.path.exists(),
    }
    if queue_info["exists"]:
        try:
            queue_info.update(llm_routes_base._LLM_JOB_QUEUE.status())
        except LLMJobQueueError as exc:
            queue_info["available"] = False
            queue_info["error"] = redact_secret_text(str(exc)).value[:500]
    try:
        governor_info = _deps._llm_governor().status()
    except LLMResourceGovernorError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_governor_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return {
        "schema_version": "bhm.llm.delegation.v1",
        "gateway_schema": GATEWAY_SCHEMA_VERSION,
        "queue_schema": LLM_JOB_QUEUE_SCHEMA_VERSION,
        "safety_policy": LLM_SAFETY_POLICY_VERSION,
        "queue": queue_info,
        "long_task": {
            "schema_version": LLM_LONG_TASK_PLAN_VERSION,
            "store": llm_routes_base._LLM_LONG_TASK_STORE.status(),
            "map_reduce": True,
            "max_chunks": LLM_LONG_TASK_MAX_CHUNKS,
            "max_fanout": LLM_LONG_TASK_MAX_FANOUT,
            "checkpoint_resume": True,
            "cache": True,
            "execution_enabled": False,
        },
        "multi_candidate": {
            "schema_version": LLM_CANDIDATE_SCHEMA_VERSION,
            "judge_version": LLM_CANDIDATE_JUDGE_VERSION,
            "roles": list(LLM_CANDIDATE_ROLES),
            "max_candidates": LLM_CANDIDATE_MAX,
            "evidence_first": True,
            "consensus_is_correctness": False,
            "execution_enabled": False,
        },
        "safe_patch": {
            "schema_version": SAFE_PATCH_SCHEMA_VERSION,
            "quarantine": True,
            "ast_context": True,
            "sandbox": True,
            "max_files": SAFE_PATCH_MAX_FILES,
            "max_diff_bytes": SAFE_PATCH_MAX_DIFF_BYTES,
            "max_timeout_seconds": SAFE_PATCH_MAX_TIMEOUT_SECONDS,
            "approval_required": True,
            "apply_enabled": False,
            "commit_enabled": False,
        },
        "local_first_policy": {
            **delegation_policy_snapshot(),
            "schema_version": LLM_DELEGATION_POLICY_VERSION,
        },
        "memory_foundry": {
            "schema_version": MEMORY_FOUNDRY_SCHEMA_VERSION,
            "preview_only": True,
            "fact_crystals": True,
            "super_crystal": True,
            "duplicate_conflict_relation_proposals": True,
            "stale_review": True,
            "cross_project_patterns": True,
            "digest_verification": True,
            "undo_window": True,
            "writes_performed": False,
            "auto_apply": False,
        },
        "retrieval_lab": {
            "schema_version": RETRIEVAL_LAB_SCHEMA_VERSION,
            "features": list(RETRIEVAL_LAB_FEATURES),
            "query_rewrite": True,
            "multi_query": True,
            "hyde": True,
            "deterministic_rerank": True,
            "hard_negatives": True,
            "synthetic_benchmark": True,
            "failure_cases": True,
            "filter_gate": True,
            "latency_gate": True,
            "leakage_gate": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "repository_intelligence": {
            "schema_version": REPOSITORY_INTELLIGENCE_SCHEMA_VERSION,
            "max_files": REPOSITORY_INTELLIGENCE_MAX_FILES,
            "file_symbol_summaries": True,
            "architectural_map": True,
            "dependency_change_impact": True,
            "test_selection_hints": True,
            "technical_debt": True,
            "issue_clustering": True,
            "source_refs": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "qa_incident_factory": {
            "schema_version": QA_INCIDENT_SCHEMA_VERSION,
            "features": list(QA_INCIDENT_FEATURES),
            "unit_property_fuzz_adversarial_drafts": True,
            "log_trace_clustering": True,
            "root_cause_hypotheses": True,
            "regression_triage": True,
            "release_security_candidates": True,
            "deterministic_verdicts": True,
            "evidence_required": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "documentation_factory": {
            "schema_version": DOCUMENTATION_FACTORY_SCHEMA_VERSION,
            "features": list(DOCUMENTATION_FACTORY_FEATURES),
            "readme_adr_changelog_release_runbook_migration": True,
            "localization": True,
            "vision_requires_confirmation": True,
            "patch_outputs": True,
            "link_section_secret_gates": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "night_shift": {
            "schema_version": NIGHT_SHIFT_SCHEMA_VERSION,
            "safe_job_types": list(NIGHT_SHIFT_SAFE_JOB_TYPES),
            "dry_run_default": True,
            "automatic_pause_on_user_activity": True,
            "automatic_pause_on_vram_temperature": True,
            "morning_report": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "model_router": {
            "schema_version": MODEL_ROUTER_SCHEMA_VERSION,
            "capabilities": list(MODEL_ROUTER_CAPABILITIES),
            "context_profiles": list(MODEL_ROUTER_CONTEXT_PROFILES),
            "measured_profile_required": True,
            "local_only_required": True,
            "cloud_fallback": False,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "cache": {
            "schema_version": LLM_CACHE_POLICY_VERSION,
            "store": llm_routes_base._LLM_CACHE_STORE.status(),
            "max_entries": LLM_CACHE_MAX_ENTRIES,
            "ttl_seconds": LLM_CACHE_DEFAULT_TTL_SECONDS,
            "key_fields": [
                "project",
                "content_digest",
                "prompt_version",
                "model_digest",
                "parameters_digest",
            ],
            "exact_result_reuse": True,
            "prefix_reuse": True,
            "invalidation": True,
            "privacy_boundary": {
                "raw_content_stored": False,
                "raw_prompt_stored": False,
                "raw_prefix_stored": False,
                "cross_project_isolation": True,
                "secret_or_injection_inputs_blocked": True,
            },
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "learning": {
            "schema_version": LLM_LEARNING_POLICY_VERSION,
            "store": llm_routes_base._LLM_LEARNING_STORE.status(),
            "max_records": LLM_LEARNING_MAX_RECORDS,
            "reviewed_only": True,
            "accepted_to_eval": True,
            "accepted_to_few_shot": True,
            "rejected_to_regression": True,
            "unverified_outputs_excluded": True,
            "raw_values_stored": False,
            "training": {
                "lora_enabled": False,
                "qlora_enabled": False,
                "training_started": False,
                "eligible": False,
                "requires_curated_dataset": True,
                "requires_baseline": True,
            },
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "governor": governor_info,
        "capabilities": {
            "submit": True,
            "status": True,
            "result": True,
            "cancel": True,
        },
        "execution_enabled": False,
        "mcp_core_tools": len(CORE_TOOL_NAMES),
        "authority": PROPOSAL_AUTHORITY,
        "auto_apply": False,
        "requires_approval": True,
    }


def register(app) -> None:
    """Attach the moved routes to the application at their original position."""

    app.get("/bhm/llm/capabilities")(bhm_llm_capabilities)
    app.post("/bhm/llm/learning/review")(bhm_llm_learning_review)
    app.get("/bhm/llm/learning")(bhm_llm_learning_status)
    app.post("/bhm/llm/learning/curate")(bhm_llm_learning_curate)
    app.post("/bhm/llm/candidates/plan")(bhm_llm_candidate_plan)
    app.post("/bhm/llm/policy/decide")(bhm_llm_delegation_decide)
    app.post("/bhm/llm/memory-foundry/preview")(bhm_llm_memory_foundry_preview)
    app.post("/bhm/llm/retrieval-lab/preview")(bhm_llm_retrieval_lab_preview)
    app.post("/bhm/llm/repository-intelligence/preview")(bhm_llm_repository_intelligence_preview)
    app.post("/bhm/llm/code-fabric/plan", include_in_schema=False)(bhm_llm_code_fabric_plan)
    app.post("/bhm/llm/qa-incident/preview")(bhm_llm_qa_incident_preview)
    app.post("/bhm/llm/documentation-factory/preview")(bhm_llm_documentation_factory_preview)
    app.post("/bhm/llm/night-shift/preview")(bhm_llm_night_shift_preview)
    app.post("/bhm/llm/model-router/decide")(bhm_llm_model_router_decide)
    app.get("/bhm/llm/model-router")(bhm_llm_model_router_snapshot)
    app.post("/bhm/llm/cache/preview")(bhm_llm_cache_preview)
    app.get("/bhm/llm/cache")(bhm_llm_cache_status)
    app.post("/bhm/llm/jobs")(bhm_llm_submit_job)
    app.get("/bhm/llm/jobs/{job_id}")(bhm_llm_job_status)
    app.get("/bhm/llm/jobs/{job_id}/result")(bhm_llm_job_result)
    app.post("/bhm/llm/jobs/{job_id}/cancel")(bhm_llm_cancel_job)
