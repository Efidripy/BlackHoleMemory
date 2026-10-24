"""Preview, planning, router, cache, and learning review routes for LLM surface.

Part of llm_routes split (FIX-017).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import app_runtime as rt
from . import llm_routes_base
from .api_models import (
    DocumentationFactoryPreviewRequest,
    LLMCachePreviewRequest,
    LLMCandidatePlanRequest,
    LLMCodeFabricPlanRequest,
    LLMDelegationDecisionRequest,
    LLMLearningCurateRequest,
    LLMLearningReviewRequest,
    MemoryDetectRequest,
    MemoryFoundryPreviewRequest,
    ModelRouterDecisionRequest,
    NightShiftPreviewRequest,
    QAIncidentPreviewRequest,
    RelationSuggestRequest,
    RepositoryIntelligencePreviewRequest,
    RetrievalLabPreviewRequest,
)
from .config import settings
from .documentation_factory import (
    DocumentationFactoryError,
    build_documentation_factory_preview,
)
from .llm_cache import (
    LLM_CACHE_POLICY_VERSION,
    LLMCacheError,
    build_cache_identity,
    build_cache_preview,
)
from .llm_candidates import CandidateError, build_candidate_plan
from .llm_code_fabric import LLMCodeFabricError, build_code_fabric_plan
from .llm_delegation_policy import DelegationPolicyError, decide_delegation
from .llm_learning import (
    LLMLearningBoundsError,
    LLMLearningCollision,
    LLMLearningError,
    LLMLearningPrivacyError,
    LLMLearningReviewError,
    LLMLearningStoreFull,
)
from .llm_routes_base import _deps, _repository_intelligence_scope
from .memory_foundry import (
    MEMORY_FOUNDRY_MAX_RECORDS,
    MemoryFoundryError,
    build_memory_foundry_preview,
)
from .model_router import ModelRouterError, route_model, router_snapshot
from .night_shift import NightShiftError, build_night_shift_preview
from .observation_security import redact_secret_text
from .qa_incident_factory import (
    QAIncidentFactoryError,
    build_qa_incident_preview,
)
from .repository_intelligence import (
    RepositoryIntelligenceError,
    build_repository_intelligence_preview,
    collect_repository_files,
)
from .retrieval_lab import RetrievalLabError, build_retrieval_lab_preview


def bhm_llm_learning_review(request: LLMLearningReviewRequest) -> dict[str, Any]:
    """Persist one explicit reviewed outcome; never start training or apply output."""

    try:
        return llm_routes_base._LLM_LEARNING_STORE.record_review(
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
        return llm_routes_base._LLM_LEARNING_STORE.status(project=project)
    except LLMLearningError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_learning_store_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc


def bhm_llm_learning_curate(request: LLMLearningCurateRequest) -> dict[str, Any]:
    """Build a reviewed eval/few-shot/regression dataset proposal only."""

    try:
        return llm_routes_base._LLM_LEARNING_STORE.curate_dataset(
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
        lookup["exact_hit"] = llm_routes_base._LLM_CACHE_STORE.get(identity, touch=False) is not None
        lookup["prefix_candidates"] = llm_routes_base._LLM_CACHE_STORE.find_prefix(identity, limit=request.prefix_limit)
    preview["lookup"] = lookup
    preview["store"] = llm_routes_base._LLM_CACHE_STORE.status()
    return preview


def bhm_llm_cache_status() -> dict[str, Any]:
    """Expose bounded cache counters and policy without returning payloads."""

    return {
        "schema_version": LLM_CACHE_POLICY_VERSION,
        "store": llm_routes_base._LLM_CACHE_STORE.status(),
        "prefix_reuse": True,
        "invalidation": True,
        "execution_enabled": False,
        "writes_performed": False,
        "auto_apply": False,
    }
