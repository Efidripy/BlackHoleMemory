"""LLM surface (/bhm/llm/*): capabilities, model router, cache, jobs, delegation and long tasks.

Moved out of app.py (FIX-017). Shared app-level callables are reached through ``_deps`` (late bound,
configured once by app.py), so monkeypatching the app names keeps working.
"""

from __future__ import annotations

import time
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse
from . import app_runtime as rt
from .app_runtime import _env_int
from .api_models import LLMJobSubmitRequest, LLMCandidatePlanRequest, LLMDelegationDecisionRequest, MemoryFoundryPreviewRequest, RetrievalLabPreviewRequest, RepositoryIntelligencePreviewRequest, LLMCodeFabricPlanRequest, QAIncidentPreviewRequest, DocumentationFactoryPreviewRequest, NightShiftPreviewRequest, ModelRouterDecisionRequest, LLMCachePreviewRequest, LLMLearningReviewRequest, LLMLearningCurateRequest, MemoryDetectRequest, RelationSuggestRequest
from .config import settings
from .llm_gateway import GATEWAY_SCHEMA_VERSION
from .mcp_surfaces import CORE_TOOL_NAMES
from .observation_security import redact_secret_text
from .llm_job_queue import LLM_JOB_QUEUE_SCHEMA_VERSION
from .llm_job_queue import LLMJobIdempotencyCollision
from .llm_job_queue import LLMJobQueue
from .llm_job_queue import LLMJobQueueError
from .llm_job_queue import LLMJobQueueFull
from .llm_job_queue import default_llm_job_queue_path
from .llm_job_queue import deterministic_llm_job_id
from .llm_long_tasks import LLM_LONG_TASK_MAX_CHUNKS
from .llm_long_tasks import LLM_LONG_TASK_MAX_FANOUT
from .llm_long_tasks import LLM_LONG_TASK_PLAN_VERSION
from .llm_long_tasks import LongTaskStore
from .llm_long_tasks import default_long_task_store_path
from .llm_candidates import LLM_CANDIDATE_JUDGE_VERSION
from .llm_candidates import LLM_CANDIDATE_MAX
from .llm_candidates import LLM_CANDIDATE_ROLES
from .llm_candidates import LLM_CANDIDATE_SCHEMA_VERSION
from .llm_candidates import CandidateError
from .llm_candidates import build_candidate_plan
from .safe_patch_factory import SAFE_PATCH_MAX_DIFF_BYTES
from .safe_patch_factory import SAFE_PATCH_MAX_FILES
from .safe_patch_factory import SAFE_PATCH_MAX_TIMEOUT_SECONDS
from .safe_patch_factory import SAFE_PATCH_SCHEMA_VERSION
from .llm_delegation_policy import LLM_DELEGATION_POLICY_VERSION
from .llm_delegation_policy import DelegationPolicyError
from .llm_delegation_policy import decide_delegation
from .llm_delegation_policy import delegation_policy_snapshot
from .memory_foundry import MEMORY_FOUNDRY_MAX_RECORDS
from .memory_foundry import MEMORY_FOUNDRY_SCHEMA_VERSION
from .memory_foundry import MemoryFoundryError
from .memory_foundry import build_memory_foundry_preview
from .retrieval_lab import RETRIEVAL_LAB_FEATURES
from .retrieval_lab import RETRIEVAL_LAB_SCHEMA_VERSION
from .retrieval_lab import RetrievalLabError
from .retrieval_lab import build_retrieval_lab_preview
from .repository_intelligence import REPOSITORY_INTELLIGENCE_MAX_FILES
from .repository_intelligence import REPOSITORY_INTELLIGENCE_SCHEMA_VERSION
from .repository_intelligence import RepositoryIntelligenceError
from .repository_intelligence import build_repository_intelligence_preview
from .repository_intelligence import collect_repository_files
from .llm_code_fabric import LLMCodeFabricError
from .llm_code_fabric import build_code_fabric_plan
from .qa_incident_factory import QA_INCIDENT_FEATURES
from .qa_incident_factory import QA_INCIDENT_SCHEMA_VERSION
from .qa_incident_factory import QAIncidentFactoryError
from .qa_incident_factory import build_qa_incident_preview
from .documentation_factory import DOCUMENTATION_FACTORY_FEATURES
from .documentation_factory import DOCUMENTATION_FACTORY_SCHEMA_VERSION
from .documentation_factory import DocumentationFactoryError
from .documentation_factory import build_documentation_factory_preview
from .night_shift import NIGHT_SHIFT_SAFE_JOB_TYPES
from .night_shift import NIGHT_SHIFT_SCHEMA_VERSION
from .night_shift import NightShiftError
from .night_shift import build_night_shift_preview
from .model_router import MODEL_ROUTER_CAPABILITIES
from .model_router import MODEL_ROUTER_CONTEXT_PROFILES
from .model_router import MODEL_ROUTER_SCHEMA_VERSION
from .model_router import ModelRouterError
from .model_router import route_model
from .model_router import router_snapshot
from .llm_cache import LLM_CACHE_DEFAULT_TTL_SECONDS
from .llm_cache import LLM_CACHE_MAX_ENTRIES
from .llm_cache import LLM_CACHE_POLICY_VERSION
from .llm_cache import LLMCacheError
from .llm_cache import LLMCacheStore
from .llm_cache import build_cache_identity
from .llm_cache import build_cache_preview
from .llm_cache import default_llm_cache_path
from .llm_learning import LLM_LEARNING_MAX_RECORDS
from .llm_learning import LLM_LEARNING_POLICY_VERSION
from .llm_learning import LLMLearningBoundsError
from .llm_learning import LLMLearningCollision
from .llm_learning import LLMLearningError
from .llm_learning import LLMLearningPrivacyError
from .llm_learning import LLMLearningReviewError
from .llm_learning import LLMLearningStore
from .llm_learning import LLMLearningStoreFull
from .llm_learning import default_llm_learning_path
from .llm_resource_governor import AdmissionRequest
from .llm_resource_governor import LLMResourceGovernorError
from .llm_safety import LLM_SAFETY_POLICY_VERSION
from .llm_safety import LLMSafetyViolation
from .llm_safety import PROPOSAL_AUTHORITY
from .llm_safety import build_proposal_envelope
from .llm_safety import sanitize_llm_value
from .llm_safety import scan_prompt_injection


class _Deps:
    """Late-bound shared callables supplied by app.py."""

    def __getattr__(self, name: str):
        raise RuntimeError(f"{__name__}: dependency {name!r} is not configured")


_deps = _Deps()


def configure(**callables) -> None:
    for key, value in callables.items():
        setattr(_deps, key, value)


_LLM_JOB_QUEUE_CAPACITY = _env_int("BHM_LLM_JOB_QUEUE_CAPACITY", 128, 1)


_LLM_JOB_QUEUE = LLMJobQueue(default_llm_job_queue_path(), capacity=_LLM_JOB_QUEUE_CAPACITY)


_LLM_LONG_TASK_STORE = LongTaskStore(default_long_task_store_path())


_LLM_CACHE_STORE = LLMCacheStore(default_llm_cache_path())


_LLM_LEARNING_STORE = LLMLearningStore(default_llm_learning_path())


def _llm_public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "job_type": job.get("job_type"),
        "project": job.get("project"),
        "priority": job.get("priority"),
        "status": job.get("status"),
        "attempts": job.get("attempts"),
        "max_attempts": job.get("max_attempts"),
        "available_at": job.get("available_at"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "last_error": redact_secret_text(str(job.get("last_error") or "")).value[:1000] or None,
        "checkpoint_digest": job.get("checkpoint_digest"),
        "result_available": job.get("result") is not None,
    }


def bhm_llm_capabilities() -> dict[str, Any]:
    """Expose the bounded Codex/plugin delegation contract without starting execution."""

    queue_info: dict[str, Any] = {
        "schema_version": LLM_JOB_QUEUE_SCHEMA_VERSION,
        "path": str(_LLM_JOB_QUEUE.path),
        "capacity": _LLM_JOB_QUEUE.capacity,
        "exists": _LLM_JOB_QUEUE.path.exists(),
    }
    if queue_info["exists"]:
        try:
            queue_info.update(_LLM_JOB_QUEUE.status())
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
            "store": _LLM_LONG_TASK_STORE.status(),
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
            "store": _LLM_CACHE_STORE.status(),
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
            "store": _LLM_LEARNING_STORE.status(),
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


def bhm_llm_learning_review(request: LLMLearningReviewRequest) -> dict[str, Any]:
    """Persist one explicit reviewed outcome; never start training or apply output."""

    try:
        return _LLM_LEARNING_STORE.record_review(
            project=request.project,
            source_job_id=request.source_job_id,
            decision=request.decision,
            reviewer=request.reviewer,
            review_reason=request.review_reason,
            input_value=request.input,
            prompt=request.prompt,
            output=request.output,
            prompt_version=request.prompt_version,
            model_digest=request.model_digest,
            parameters=request.parameters,
            validation=request.validation,
            provenance=request.provenance,
        )
    except LLMLearningCollision as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "llm_learning_review_collision", "source_job_id": exc.source_job_id},
        ) from exc
    except LLMLearningStoreFull as exc:
        raise HTTPException(
            status_code=429,
            detail={"error": "llm_learning_store_full", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "30"},
        ) from exc
    except (LLMLearningPrivacyError, LLMLearningReviewError, LLMLearningBoundsError, LLMLearningError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_learning_review_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc


def bhm_llm_learning_status(project: str | None = None) -> dict[str, Any]:
    """Expose reviewed-learning counters and the fail-closed training gate."""

    try:
        return _LLM_LEARNING_STORE.status(project=project)
    except LLMLearningError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_learning_store_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc


def bhm_llm_learning_curate(request: LLMLearningCurateRequest) -> dict[str, Any]:
    """Build a reviewed eval/few-shot/regression dataset proposal only."""

    try:
        return _LLM_LEARNING_STORE.curate_dataset(
            project=request.project,
            limit=request.limit,
            include_payload=request.include_payload,
        )
    except LLMLearningError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_learning_curation_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc


def bhm_llm_candidate_plan(request: LLMCandidatePlanRequest) -> dict[str, Any]:
    """Build an evidence-first candidate plan without starting model execution."""

    try:
        plan = build_candidate_plan(
            request.task_id,
            request.objective,
            project=request.project,
            roles=request.roles,
            candidate_count=request.candidate_count,
            prompt_version=request.prompt_version,
            model_digest=request.model_digest,
        )
    except CandidateError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_candidate_plan_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return plan.as_dict()


def bhm_llm_delegation_decide(request: LLMDelegationDecisionRequest) -> dict[str, Any]:
    """Return a deterministic local/Codex/operator escalation decision."""

    try:
        decision = decide_delegation(
            request.task_type,
            confidence=request.confidence,
            sensitivity=request.sensitivity,
            mutation_requested=request.mutation_requested,
            evidence_count=request.evidence_count,
            local_capabilities=request.local_capabilities,
            risk_flags=request.risk_flags,
            operator_approved=request.operator_approved,
        )
    except DelegationPolicyError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_delegation_policy_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return decision.as_dict()


def bhm_llm_memory_foundry_preview(request: MemoryFoundryPreviewRequest) -> dict[str, Any]:
    """Compose bounded memory-consolidation proposals without performing writes."""

    all_records = rt._load_live_memories()
    records = [
        item
        for item in all_records
        if _deps._memory_matches_filters(item, project=request.project, include_archived=False)
    ]
    if request.memory_ids is not None:
        selected = {str(item) for item in request.memory_ids}
        records = [item for item in records if str(item.get("source_id") or "") in selected]
    records = sorted(
        records,
        key=lambda item: (str(item.get("updated_at") or item.get("created_at") or ""), str(item.get("source_id") or "")),
        reverse=True,
    )[:MEMORY_FOUNDRY_MAX_RECORDS]
    detection_request = MemoryDetectRequest(
        project=request.project,
        limit=min(max(request.limit * 2, 20), 200),
        include_archived=False,
    )
    try:
        preview = build_memory_foundry_preview(
            records,
            project=rt._canonical_project(request.project),
            cross_project_records=all_records if request.memory_ids is None else records,
            duplicate_candidates=_deps._detect_duplicates(detection_request),
            conflict_candidates=_deps._detect_conflicts(detection_request),
            relation_candidates=_deps._relation_suggest(
                RelationSuggestRequest(project=request.project, limit=detection_request.limit)
            ).get("suggestions", []),
            stale_days=request.stale_days,
            undo_window_seconds=request.undo_window_seconds,
            limit=request.limit,
        )
    except MemoryFoundryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "memory_foundry_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


async def bhm_llm_retrieval_lab_preview(request: RetrievalLabPreviewRequest) -> dict[str, Any]:
    """Build retrieval experiments and gates without mutating the live path."""

    candidates = request.candidates
    observed_latency_ms: float | None = None
    if candidates is None and request.use_live_candidates:
        started = time.perf_counter()
        try:
            await rt._ensure_provider_warmup_ready()
            hits, _total = await _deps.federated_search(
                request.query,
                rt._canonical_project(request.project),
                limit=min(max(request.limit * 4, 20), 200),
            )
            candidates = hits
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "retrieval_lab_source_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            ) from exc
        finally:
            observed_latency_ms = (time.perf_counter() - started) * 1000.0
    if candidates is None:
        candidates = []
    try:
        preview = build_retrieval_lab_preview(
            request.query,
            project=rt._canonical_project(request.project),
            candidates=candidates,
            feature_flags=request.feature_flags,
            limit=request.limit,
            benchmark_cases=request.benchmark_cases,
            latency_budget_ms=request.latency_budget_ms,
            observed_latency_ms=observed_latency_ms,
        )
    except RetrievalLabError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "retrieval_lab_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


def _repository_intelligence_scope(raw_root: str) -> str:
    """Normalize a request scope without turning it into a filesystem path."""

    raw = str(raw_root or ".").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or len(raw) > 240
        or "\x00" in raw
        or path.is_absolute()
        or re.match(r"^[A-Za-z]:", raw)
        or ".." in path.parts
    ):
        raise HTTPException(status_code=422, detail={"error": "repository_root_outside_allowlist"})
    return "." if path.as_posix() == "." else path.as_posix().strip("/")


def bhm_llm_repository_intelligence_preview(request: RepositoryIntelligencePreviewRequest) -> dict[str, Any]:
    """Analyze a bounded repository snapshot without writing source or Git state."""

    source_files = request.files
    if source_files is None:
        base = Path(settings.repo_root)
        scope = _repository_intelligence_scope(request.root)
        try:
            source_files = collect_repository_files(base, request.paths, scope=scope)
        except RepositoryIntelligenceError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "repository_intelligence_source_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
            ) from exc
    try:
        preview = build_repository_intelligence_preview(
            source_files or [],
            project=request.project,
            changed_paths=request.changed_paths,
            include_tests=request.include_tests,
            max_files=request.max_files,
        )
    except RepositoryIntelligenceError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "repository_intelligence_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


def bhm_llm_code_fabric_plan(request: LLMCodeFabricPlanRequest) -> dict[str, Any]:
    """Internal proposal-only code-intelligence LLM plan."""

    try:
        return build_code_fabric_plan(
            request.task_type,
            request.payload,
            project=rt._canonical_project(request.project),
            context_digest=request.context_digest,
            required_capabilities=request.required_capabilities,
            context_tokens=request.context_tokens,
            sensitivity=request.sensitivity,
            mutation_requested=request.mutation_requested,
            confidence=request.confidence,
            evidence_count=request.evidence_count,
            risk_flags=request.risk_flags,
            operator_approved=request.operator_approved,
        )
    except LLMCodeFabricError as exc:
        raise HTTPException(status_code=422, detail={"error": "llm_code_fabric_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


def bhm_llm_qa_incident_preview(request: QAIncidentPreviewRequest) -> dict[str, Any]:
    """Build QA/incident proposals and deterministic evidence without execution."""

    try:
        preview = build_qa_incident_preview(
            request.artifacts,
            project=request.project,
            changed_paths=request.changed_paths,
            release_candidate=request.release_candidate,
            feature_flags=request.feature_flags,
            max_items=request.max_items,
        )
    except QAIncidentFactoryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "qa_incident_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


def bhm_llm_documentation_factory_preview(request: DocumentationFactoryPreviewRequest) -> dict[str, Any]:
    """Build documentation/ops/vision patches without writing or OCR execution."""

    try:
        preview = build_documentation_factory_preview(
            request.documents,
            project=request.project,
            locale=request.locale,
            vision_confirmed=request.vision_confirmed,
            vision_assets=request.vision_assets,
            feature_flags=request.feature_flags,
            max_patches=request.max_patches,
        )
    except DocumentationFactoryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "documentation_factory_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


def bhm_llm_night_shift_preview(request: NightShiftPreviewRequest) -> dict[str, Any]:
    """Plan safe queued jobs with resource pauses; never start the worker."""

    try:
        preview = build_night_shift_preview(
            request.jobs,
            resource_snapshot=request.resource_snapshot,
            maintenance_window_open=request.maintenance_window_open,
            user_active=request.user_active,
            dry_run=request.dry_run,
            max_jobs=request.max_jobs,
        )
    except NightShiftError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "night_shift_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


def bhm_llm_model_router_decide(request: ModelRouterDecisionRequest) -> dict[str, Any]:
    """Return a local capability/profile route or a fail-closed rejection."""

    try:
        decision = route_model(
            request.task_type,
            required_capabilities=request.required_capabilities,
            context_tokens=request.context_tokens,
            measurements=request.measurements,
            models=request.models,
        )
    except ModelRouterError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "model_router_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return decision.as_dict()


def bhm_llm_model_router_snapshot() -> dict[str, Any]:
    """Expose local router capabilities and measured-profile status."""

    return router_snapshot()


def bhm_llm_cache_preview(request: LLMCachePreviewRequest) -> dict[str, Any]:
    """Build a cache/prefix/invalidation proposal without persisting input or result."""

    try:
        identity = build_cache_identity(
            request.content,
            request.prompt,
            project=request.project,
            prompt_version=request.prompt_version,
            model_digest=request.model_digest,
            parameters=request.parameters,
            prompt_prefix=request.prompt_prefix,
        )
        preview = build_cache_preview(
            identity,
            result=request.result,
            result_supplied=request.result_supplied,
        )
    except LLMCacheError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_cache_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    lookup: dict[str, Any] = {
        "inspected": bool(request.inspect_store),
        "exact_hit": False,
        "prefix_candidates": [],
    }
    if request.inspect_store and identity.cacheable:
        lookup["exact_hit"] = _LLM_CACHE_STORE.get(identity, touch=False) is not None
        lookup["prefix_candidates"] = _LLM_CACHE_STORE.find_prefix(identity, limit=request.prefix_limit)
    preview["lookup"] = lookup
    preview["store"] = _LLM_CACHE_STORE.status()
    return preview


def bhm_llm_cache_status() -> dict[str, Any]:
    """Expose bounded cache counters and policy without returning payloads."""

    return {
        "schema_version": LLM_CACHE_POLICY_VERSION,
        "store": _LLM_CACHE_STORE.status(),
        "prefix_reuse": True,
        "invalidation": True,
        "execution_enabled": False,
        "writes_performed": False,
        "auto_apply": False,
    }


def bhm_llm_submit_job(request: LLMJobSubmitRequest) -> dict[str, Any]:
    """Persist a sanitized delegation request; a worker is intentionally not started here."""

    try:
        safe = sanitize_llm_value(
            request.payload,
            source="llm-delegation-ingress",
            project=request.project,
        )
    except LLMSafetyViolation as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "llm_ingress_rejected",
                "policy_version": LLM_SAFETY_POLICY_VERSION,
                "detail": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc

    injection_findings = list(
        scan_prompt_injection(json.dumps(safe.value, ensure_ascii=False, default=str))
    )
    safety_provenance = dict(safe.provenance)
    safety_provenance["injection_findings"] = injection_findings
    stored_payload = {
        "contract_version": "bhm.llm.delegation.v1",
        "input": safe.value,
        "safety": safety_provenance,
    }
    try:
        job_id = deterministic_llm_job_id(request.idempotency_key)
        governor = _deps._llm_governor()
        decision = governor.admit(
            AdmissionRequest(
                job_id=job_id,
                workload=request.workload,
                max_wall_seconds=request.max_wall_seconds,
                max_output_tokens=request.max_output_tokens,
            )
        )
    except LLMResourceGovernorError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_governor_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc

    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": "llm_admission_denied", "decision": decision.as_dict()},
            headers={"Retry-After": "5"},
        )

    try:
        try:
            enqueue_result = _LLM_JOB_QUEUE.enqueue(
                idempotency_key=request.idempotency_key,
                job_type=request.job_type,
                payload=stored_payload,
                project=request.project,
                priority=request.priority,
                max_attempts=request.max_attempts,
            )
        except LLMJobQueueFull as exc:
            raise HTTPException(
                status_code=429,
                detail={"error": "llm_job_queue_full", "pending": exc.pending, "capacity": exc.capacity},
                headers={"Retry-After": "5"},
            ) from exc
        except LLMJobIdempotencyCollision as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "llm_job_idempotency_collision", "idempotency_key": exc.idempotency_key},
            ) from exc
        except LLMJobQueueError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
                headers={"Retry-After": "5"},
            ) from exc
    finally:
        # P17.7 exposes durable delegation only; no worker reservation is held.
        governor.release(job_id)

    job = _LLM_JOB_QUEUE.get(enqueue_result.job_id)
    if job is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_disappeared", "job_id": enqueue_result.job_id},
        )
    return {
        "accepted": True,
        "inserted": enqueue_result.inserted,
        "execution_enabled": False,
        "job": _llm_public_job(job),
        "admission": {
            "decision": decision.as_dict(),
            "reservation_held": False,
        },
        "safety": {
            "policy_version": LLM_SAFETY_POLICY_VERSION,
            "redaction_count": safe.provenance.get("redaction_count", 0),
            "injection_findings": injection_findings,
            "authority": PROPOSAL_AUTHORITY,
            "auto_apply": False,
            "requires_approval": True,
        },
    }


def bhm_llm_job_status(job_id: str, request: Request) -> dict[str, Any]:
    try:
        job = _LLM_JOB_QUEUE.get(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    _deps._authorize_resource_project(request, job.get("project"))
    return {"job": _llm_public_job(job), "execution_enabled": False}


def bhm_llm_job_result(job_id: str, request: Request) -> Any:
    try:
        job = _LLM_JOB_QUEUE.get(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    _deps._authorize_resource_project(request, job.get("project"))
    status = str(job.get("status") or "")
    if status in {"queued", "processing"}:
        return JSONResponse(
            status_code=202,
            content={
                "job_id": job_id,
                "status": status,
                "result_available": False,
                "execution_enabled": False,
            },
        )
    if status != "completed":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "llm_job_result_unavailable",
                "job_id": job_id,
                "status": status,
                "last_error": redact_secret_text(str(job.get("last_error") or "")).value[:500] or None,
            },
        )
    try:
        result = job.get("result")
        proposal = (
            result
            if isinstance(result, dict) and result.get("authority") == PROPOSAL_AUTHORITY
            else build_proposal_envelope(
                job_id=job_id,
                output=result,
                provenance={
                    "project": job.get("project", "blackholememory"),
                    "source": "llm-delegation-result",
                    "queue_schema": LLM_JOB_QUEUE_SCHEMA_VERSION,
                },
            )
        )
    except LLMSafetyViolation as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "llm_result_safety_validation_failed",
                "policy_version": LLM_SAFETY_POLICY_VERSION,
                "detail": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc
    return {
        "job_id": job_id,
        "status": "completed",
        "result": proposal,
        "authority": PROPOSAL_AUTHORITY,
        "auto_apply": False,
        "requires_approval": True,
        "execution_enabled": False,
    }


def bhm_llm_cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    try:
        existing_job = _LLM_JOB_QUEUE.get(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    if existing_job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    _deps._authorize_resource_project(request, existing_job.get("project"))
    try:
        job = _LLM_JOB_QUEUE.cancel(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    _deps._llm_governor().release(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    return {"cancelled": str(job.get("status")) == "cancelled", "execution_enabled": False, "job": _llm_public_job(job)}



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
