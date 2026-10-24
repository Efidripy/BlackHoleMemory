"""Domain module app_governed: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

from typing import Any
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from . import app_runtime as rt
from .app_runtime import _bounded_read, _bounded_write, _run_bounded_read, _safe_exception_text
from .api_models import (
    ConsolidationChangeSetPreviewRequest,
    ConsolidationChangeSetReviewRequest,
    GovernedConsolidationApplyRequest,
    GovernedConsolidationCreateRequest,
    GovernedConsolidationDecisionRequest,
    GovernedConsolidationProposalRequest,
    GovernedSemanticProposalRequest,
    SharedMemoryPolicyPreflightRequest,
    SharedMemoryReadRequest,
)
from .config import settings
from .caller_auth import authorize_projects
from .consolidation_change_set import ConsolidationChangeSetError
from .consolidation_change_set import build_consolidation_change_set_preview
from .consolidation_review import ConsolidationReviewError
from .consolidation_review import append_consolidation_review
from .consolidation_review import build_consolidation_review
from .governed_consolidation import GovernedConsolidationError
from .governed_consolidation import GovernedConsolidationRepository
from .governed_consolidation import analyze_records as analyze_governed_consolidation_records
from .governed_consolidation import apply_approved_proposal
from .governed_consolidation import build_proposal as build_governed_consolidation_proposal
from .governed_consolidation import dry_run_apply as dry_run_governed_consolidation_apply
from .governed_consolidation import runtime_status as governed_consolidation_status
from .governed_consolidation import validate_proposal_current as validate_governed_consolidation_proposal
from .governed_auto_review import policy_status as governed_auto_review_policy_status
from .governed_auto_review import operator_consent_required as governed_operator_consent_required
from .governed_semantic_editor import SemanticEditorConfig
from .memory_doctor import MemoryDoctorSnapshotError
from .memory_doctor import load_authoritative_sqlite_snapshot
from .memory_doctor import run_authoritative_sqlite_memory_doctor
from .runtime_storage import resolve_runtime_storage_config
from .memory_service import MemoryServiceNotReady
from . import app_core_hook
from . import app_governed_base as _base

from .app_governed_base import (  # noqa: F401
    _governed_consolidation_error,
    _governed_consolidation_project,
    _require_governed_consolidation_enabled,
    _semantic_graph_node_id,
)
from .app_governed_shared import (  # noqa: F401
    _canonical_shared_memory_attributes,
    _serialize_governed_shared_memory,
    _shared_memory_policy_preflight,
    _shared_memory_read,
)
from .app_governed_semantic import (  # noqa: F401
    GOVERNED_SEMANTIC_RETRY_MAX_ATTEMPTS,
    GOVERNED_SEMANTIC_RETRY_SCHEMA_VERSION,
    _classify_semantic_retry,
    _governed_semantic_lexical_fallback_records,
    _governed_semantic_proposal,
    _governed_semantic_retry_limit,
    _governed_semantic_shadow_metrics,
    _retry_attempt_receipt,
    _retry_receipt,
    _run_governed_semantic_with_retries,
    _semantic_proposal_candidate_digest,
)


def _utility_feedback_change_set_preview(
    request: ConsolidationChangeSetPreviewRequest,
) -> dict[str, Any]:
    """Build a snapshot-bound consolidation preview without an apply path."""

    canonical_project = rt._canonical_project(request.project)
    report = app_core_hook._utility_feedback_report(
        project=canonical_project,
        as_of=request.as_of,
        half_life_days=30.0,
        min_samples=3,
    )
    # `_utility_feedback_report` adds route-level project/side-effect fields
    # after computing the immutable utility digest.  Pass only the signed
    # utility-report envelope into the pure change-set contract.
    utility_source = {
        key: report[key]
        for key in ("schema_version", "as_of", "half_life_days", "min_samples", "rows", "execution", "report_digest")
        if key in report
    }
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    try:
        snapshot = load_authoritative_sqlite_snapshot(
            database_path,
            project=canonical_project,
            limit=10_000,
        )
        doctor = run_authoritative_sqlite_memory_doctor(
            database_path,
            project=canonical_project,
            limit=10_000,
        )
        preview = build_consolidation_change_set_preview(
            utility_source,
            project=canonical_project,
            authority_snapshot=snapshot,
            candidates=request.candidates,
            doctor_report=doctor,
            as_of=request.as_of,
            max_actions=request.max_actions,
        )
    except (ConsolidationChangeSetError, MemoryDoctorSnapshotError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "consolidation_change_set_preview_invalid", "reason": _safe_exception_text(exc)},
        ) from exc
    return {
        **preview,
        "side_effects": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "projection_mutation": False,
            "model_called": False,
            "automatic_lifecycle_change": False,
        },
    }


def _record_consolidation_change_set_review(
    request: ConsolidationChangeSetReviewRequest,
    *,
    principal: Any,
) -> dict[str, Any]:
    """Persist a caller-bound decision after regenerating the exact preview.

    Approval is intentionally a non-executable review statement. A future
    apply implementation must not consume this artifact as authority to write.
    """

    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    project = rt._canonical_project(request.project)
    project_error = authorize_projects(principal, (project,), require_explicit=True)
    if project_error:
        raise HTTPException(status_code=403, detail={"code": project_error})
    preview_request = ConsolidationChangeSetPreviewRequest(
        project=project,
        as_of=request.as_of,
        candidates=request.candidates,
        max_actions=request.max_actions,
    )
    regenerated = _utility_feedback_change_set_preview(preview_request)
    if request.change_set != regenerated:
        raise HTTPException(
            status_code=409,
            detail={"code": "consolidation_change_set_stale_or_tampered"},
        )
    try:
        review = build_consolidation_review(
            regenerated,
            review_id=request.review_id,
            decision=request.decision,
            action_ids=request.action_ids,
            reviewer_id=str(principal.caller_id),
            reviewed_at=request.reviewed_at,
            rationale_digest=request.rationale_digest,
        )
        _artifact, inserted = append_consolidation_review(rt._memory_service(), review)
    except (ConsolidationReviewError, MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "consolidation_change_set_review_invalid", "reason": _safe_exception_text(exc)},
        ) from exc
    return {
        "schema_version": "bhm.consolidation.change-set-review.ack.v1",
        "project": review.project,
        "review_id": review.review_id,
        "change_set_digest": review.change_set_digest,
        "authority_snapshot_digest": review.authority_snapshot_digest,
        "action_ids": list(review.action_ids),
        "decision": review.decision,
        "inserted": inserted,
        "replayed": not inserted,
        "apply_performed": False,
        "lifecycle_action": "none",
        "side_effects": {
            "sqlite_mutation": bool(inserted),
            "memory_lifecycle_mutation": False,
            "qdrant_mutation": False,
            "projection_mutation": False,
            "mem0_mutation": False,
            "automatic_lifecycle_change": False,
        },
    }


def _governed_consolidation_create(request: GovernedConsolidationCreateRequest, *, principal: Any) -> dict[str, Any]:
    _base._require_governed_consolidation_enabled()
    project = _base._governed_consolidation_project(principal, request.project)
    memory_ids = list(dict.fromkeys(str(item).strip() for item in request.memory_ids if str(item).strip()))
    if len(memory_ids) != len(request.memory_ids):
        raise HTTPException(status_code=422, detail={"code": "governed_consolidation_memory_ids_invalid"})
    try:
        service = rt._memory_service()
        records = service.get_records(memory_ids, project=project)
        if len(records) != len(memory_ids):
            raise GovernedConsolidationError("one or more canonical basis records are missing or cross-project")
        if request.candidate is None:
            proposal = analyze_governed_consolidation_records(project=project, records=records, operation=request.operation)
        else:
            proposal = build_governed_consolidation_proposal(
                project=project,
                records=records,
                operation=request.operation,
                candidate=request.candidate,
                reason=request.reason or "operator-submitted same-project consolidation candidate",
                confidence=request.confidence,
            )
        stored, inserted = GovernedConsolidationRepository(app_core_hook._governed_consolidation_database_path()).create(proposal)
    except (GovernedConsolidationError, MemoryServiceNotReady, OSError, ValueError) as exc:
        raise _base._governed_consolidation_error(exc) from exc
    return {
        "proposal": stored,
        "inserted": inserted,
        "replayed": not inserted,
        "side_effects": {"sqlite_mutation": bool(inserted), "memory_lifecycle_mutation": False, "memory_outbox_mutation": False, "qdrant_mutation": False, "mem0_mutation": False},
    }


def _governed_consolidation_inspect(*, project: str, proposal_id: str, principal: Any) -> dict[str, Any]:
    _base._require_governed_consolidation_enabled()
    try:
        proposal = GovernedConsolidationRepository(app_core_hook._governed_consolidation_database_path()).get(proposal_id, project=_base._governed_consolidation_project(principal, project))
    except GovernedConsolidationError as exc:
        raise _base._governed_consolidation_error(exc) from exc
    return {"proposal": proposal, "side_effects": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def _governed_consolidation_list(*, project: str, status: str | None, limit: int, principal: Any) -> dict[str, Any]:
    _base._require_governed_consolidation_enabled()
    canonical_project = _base._governed_consolidation_project(principal, project)
    try:
        proposals = GovernedConsolidationRepository(app_core_hook._governed_consolidation_database_path()).list(project=canonical_project, status=status, limit=limit)
    except GovernedConsolidationError as exc:
        raise _base._governed_consolidation_error(exc) from exc
    return {"project": canonical_project, "proposals": proposals, "count": len(proposals), "side_effects": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def _governed_consolidation_validate(*, project: str, proposal_id: str, principal: Any) -> dict[str, Any]:
    _base._require_governed_consolidation_enabled()
    try:
        proposal = GovernedConsolidationRepository(app_core_hook._governed_consolidation_database_path()).get(proposal_id, project=_base._governed_consolidation_project(principal, project))
        return validate_governed_consolidation_proposal(proposal=proposal, repository=rt._memory_service().repository)
    except (GovernedConsolidationError, MemoryServiceNotReady, OSError, ValueError) as exc:
        raise _base._governed_consolidation_error(exc) from exc


def _governed_consolidation_dry_run(request: GovernedConsolidationProposalRequest, *, principal: Any) -> dict[str, Any]:
    _base._require_governed_consolidation_enabled()
    try:
        proposal = GovernedConsolidationRepository(app_core_hook._governed_consolidation_database_path()).get(request.proposal_id, project=_base._governed_consolidation_project(principal, request.project))
        return dry_run_governed_consolidation_apply(proposal=proposal, repository=rt._memory_service().repository)
    except (GovernedConsolidationError, MemoryServiceNotReady, OSError, ValueError) as exc:
        raise _base._governed_consolidation_error(exc) from exc


def _governed_consolidation_decide(request: GovernedConsolidationDecisionRequest, *, principal: Any) -> dict[str, Any]:
    _base._require_governed_consolidation_enabled()
    project = _base._governed_consolidation_project(principal, request.project)
    try:
        proposal = GovernedConsolidationRepository(app_core_hook._governed_consolidation_database_path()).decide(
            proposal_id=request.proposal_id,
            project=project,
            decision=request.decision,
            actor=str(principal.caller_id),
            semantic_attestation=request.semantic_attestation,
        )
    except GovernedConsolidationError as exc:
        raise _base._governed_consolidation_error(exc) from exc
    return {"proposal": proposal, "apply_performed": False, "side_effects": {"sqlite_mutation": True, "memory_lifecycle_mutation": False, "memory_outbox_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def _governed_consolidation_apply(request: GovernedConsolidationApplyRequest, *, principal: Any) -> dict[str, Any]:
    _base._require_governed_consolidation_enabled()
    project = _base._governed_consolidation_project(principal, request.project)
    try:
        result = apply_approved_proposal(database_path=app_core_hook._governed_consolidation_database_path(), proposal_id=request.proposal_id, project=project, apply=request.apply, confirmation=request.confirmation)
    except (GovernedConsolidationError, OSError, ValueError) as exc:
        raise _base._governed_consolidation_error(exc) from exc
    return {"proposal_id": result.proposal_id, "status": result.status, "memory_ids": list(result.memory_ids), "outbox_event_ids": list(result.outbox_event_ids), "link_id": result.link_id, "observability": {"apply_duration_ms": result.apply_duration_ms, "outbox": result.outbox}, "side_effects": {"sqlite_mutation": True, "memory_lifecycle_mutation": bool(result.memory_ids), "memory_outbox_mutation": bool(result.outbox_event_ids), "qdrant_mutation": False, "mem0_mutation": False, "projection": "existing_outbox_projector"}}


async def bhm_shared_memory_policy_evaluate(
    request: SharedMemoryPolicyPreflightRequest,
    http_request: Request,
) -> dict:
    principal = getattr(http_request.state, "bhm_caller_principal", None)
    auth_kind = str(getattr(http_request.state, "bhm_auth_kind", "") or "")
    return await rt._run_bounded_write(
        "bhm.shared_memory.policy_preflight",
        _shared_memory_policy_preflight,
        request,
        principal=principal,
        auth_kind=auth_kind,
    )


async def bhm_shared_memory_read(
    request: SharedMemoryReadRequest,
    http_request: Request,
) -> dict[str, Any]:
    return await rt._run_bounded_write(
        "bhm.shared_memory.read",
        _shared_memory_read,
        request,
        principal=getattr(http_request.state, "bhm_caller_principal", None),
        auth_kind=str(getattr(http_request.state, "bhm_auth_kind", "") or ""),
    )


async def bhm_consolidation_change_set_preview(
    request: ConsolidationChangeSetPreviewRequest,
) -> dict[str, Any]:
    """Return a caller-scoped, snapshot-bound, non-executable change-set."""

    return await _run_bounded_read(
        "bhm.consolidation.change_set_preview",
        _utility_feedback_change_set_preview,
        request,
    )


async def bhm_consolidation_change_set_review(
    request: ConsolidationChangeSetReviewRequest,
    http_request: Request,
) -> dict[str, Any]:
    """Record an admin-gated review; it never applies a change-set."""

    return await rt._run_bounded_write(
        "bhm.consolidation.change_set_review",
        _record_consolidation_change_set_review,
        request,
        principal=getattr(http_request.state, "bhm_caller_principal", None),
    )


async def bhm_governed_consolidation_status() -> dict[str, Any]:
    """Return the governed mode and auto-review policy without a write."""

    result = governed_consolidation_status(app_core_hook._governed_consolidation_database_path())
    auto_review = governed_auto_review_policy_status()
    result["auto_review_apply"] = auto_review
    if result.get("enabled") and auto_review["automatic_apply_enabled"]:
        result["state"] = "policy-auto-reviewed"
        result["mode"] = "policy-auto-reviewed"
    elif result.get("enabled") and governed_operator_consent_required():
        result["state"] = "operator-consent"
        result["mode"] = "operator-consent"
    config = SemanticEditorConfig.from_env()
    result["semantic_editor"] = {
        "schema_version": "bhm.governed-semantic-editor.v1",
        "enabled": config.enabled,
        "mode": "local-model-proposal-only",
        "candidate_retrieval": "federated-then-sqlite-revalidated",
        "max_retrieval_candidates": 20,
        "model_id": config.model_id or None,
        "direct_mem0_writes": False,
        "direct_qdrant_writes": False,
        "automatic_apply": False,
        "automatic_apply_reason": "semantic_evidence_requires_operator_review",
        "operator_consent_required": governed_operator_consent_required(),
    }
    return result


async def bhm_governed_consolidation_create(request: GovernedConsolidationCreateRequest, http_request: Request) -> dict[str, Any]:
    return await rt._run_bounded_write("bhm.governed_consolidation.create", _governed_consolidation_create, request, principal=getattr(http_request.state, "bhm_caller_principal", None))


async def bhm_governed_semantic_proposal(request: GovernedSemanticProposalRequest, http_request: Request) -> dict[str, Any]:
    """Return a preview or store one local proposal for governed processing."""

    # Preview remains read-only. A stored local-model proposal can be routed
    # through the separately feature-gated policy review/apply path; it never
    # gains direct Mem0 or Qdrant write authority.
    principal = getattr(http_request.state, "bhm_caller_principal", None)
    if request.store_proposal:
        async with _bounded_write("bhm.governed_consolidation.semantic_proposal"):
            return await _governed_semantic_proposal(request, principal=principal)
    async with _bounded_read("bhm.governed_consolidation.semantic_proposal"):
        return await _governed_semantic_proposal(request, principal=principal)


async def bhm_governed_semantic_shadow_metrics(
    http_request: Request,
    project: str = Query(min_length=1, max_length=160),
) -> dict[str, Any]:
    return await _run_bounded_read(
        "bhm.governed_consolidation.semantic_shadow_metrics",
        _governed_semantic_shadow_metrics,
        project=project,
        principal=getattr(http_request.state, "bhm_caller_principal", None),
    )


async def bhm_governed_consolidation_list(
    http_request: Request,
    project: str = Query(min_length=1, max_length=160),
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return await _run_bounded_read("bhm.governed_consolidation.list", _governed_consolidation_list, project=project, status=status, limit=limit, principal=getattr(http_request.state, "bhm_caller_principal", None))


async def bhm_governed_consolidation_inspect(
    http_request: Request,
    proposal_id: str,
    project: str = Query(min_length=1, max_length=160),
) -> dict[str, Any]:
    return await _run_bounded_read("bhm.governed_consolidation.inspect", _governed_consolidation_inspect, project=project, proposal_id=proposal_id, principal=getattr(http_request.state, "bhm_caller_principal", None))


async def bhm_governed_consolidation_validate(
    http_request: Request,
    proposal_id: str,
    project: str = Query(min_length=1, max_length=160),
) -> dict[str, Any]:
    return await _run_bounded_read("bhm.governed_consolidation.validate", _governed_consolidation_validate, project=project, proposal_id=proposal_id, principal=getattr(http_request.state, "bhm_caller_principal", None))


async def bhm_governed_consolidation_dry_run(request: GovernedConsolidationProposalRequest, http_request: Request) -> dict[str, Any]:
    return await _run_bounded_read("bhm.governed_consolidation.dry_run", _governed_consolidation_dry_run, request, principal=getattr(http_request.state, "bhm_caller_principal", None))


async def bhm_governed_consolidation_decide(
    request: GovernedConsolidationDecisionRequest,
    http_request: Request,
) -> dict[str, Any]:
    return await rt._run_bounded_write("bhm.governed_consolidation.decision", _governed_consolidation_decide, request, principal=getattr(http_request.state, "bhm_caller_principal", None))


async def bhm_governed_consolidation_apply(
    request: GovernedConsolidationApplyRequest,
    http_request: Request,
) -> dict[str, Any]:
    return await rt._run_bounded_write("bhm.governed_consolidation.apply", _governed_consolidation_apply, request, principal=getattr(http_request.state, "bhm_caller_principal", None))



def register(app) -> None:
    """Attach this module's routes to the application."""

    app.post("/bhm/shared-memory/policy/evaluate")(bhm_shared_memory_policy_evaluate)
    app.post("/bhm/shared-memory/read")(bhm_shared_memory_read)
    app.post("/bhm/consolidation/change-set/preview")(bhm_consolidation_change_set_preview)
    app.post("/bhm/consolidation/change-set/review")(bhm_consolidation_change_set_review)
    app.get("/bhm/governed-consolidation/status")(bhm_governed_consolidation_status)
    app.post("/bhm/governed-consolidation/proposals")(bhm_governed_consolidation_create)
    app.post("/bhm/governed-consolidation/semantic-proposals")(bhm_governed_semantic_proposal)
    app.get("/bhm/governed-consolidation/semantic-shadow-metrics")(bhm_governed_semantic_shadow_metrics)
    app.get("/bhm/governed-consolidation/proposals")(bhm_governed_consolidation_list)
    app.get("/bhm/governed-consolidation/proposals/{proposal_id}")(bhm_governed_consolidation_inspect)
    app.get("/bhm/governed-consolidation/proposals/{proposal_id}/validate")(bhm_governed_consolidation_validate)
    app.post("/bhm/governed-consolidation/proposals/dry-run")(bhm_governed_consolidation_dry_run)
    app.post("/bhm/governed-consolidation/proposals/decision")(bhm_governed_consolidation_decide)
    app.post("/bhm/governed-consolidation/proposals/apply")(bhm_governed_consolidation_apply)
