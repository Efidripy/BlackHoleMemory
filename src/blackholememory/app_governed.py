"""Domain module app_governed: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections import Counter
from typing import Any, Mapping
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from . import app_runtime as rt
from .app_runtime import ResponseTimeout, _bounded_read, _bounded_write, _run_bounded_read, _safe_exception_text
from .api_models import ConsolidationChangeSetPreviewRequest, ConsolidationChangeSetReviewRequest, GovernedConsolidationCreateRequest, GovernedSemanticProposalRequest, GovernedConsolidationDecisionRequest, GovernedConsolidationProposalRequest, GovernedConsolidationApplyRequest, SharedMemoryPolicyPreflightRequest, SharedMemoryReadRequest
from .config import settings
from .caller_auth import authorize_projects
from .mem0_adapter import StorageNotReady
from .consolidation_change_set import ConsolidationChangeSetError
from .consolidation_change_set import build_consolidation_change_set_preview
from .consolidation_review import ConsolidationReviewError
from .consolidation_review import append_consolidation_review
from .consolidation_review import build_consolidation_review
from .governed_consolidation import GovernedConsolidationApprovalRequired
from .governed_consolidation import GovernedConsolidationError
from .governed_consolidation import GovernedConsolidationMigrationRequired
from .governed_consolidation import GovernedConsolidationRepository
from .governed_consolidation import GovernedConsolidationStale
from .governed_consolidation import analyze_records as analyze_governed_consolidation_records
from .governed_consolidation import apply_approved_proposal
from .governed_consolidation import build_proposal as build_governed_consolidation_proposal
from .governed_consolidation import dry_run_apply as dry_run_governed_consolidation_apply
from .governed_consolidation import runtime_enabled as governed_consolidation_enabled
from .governed_consolidation import runtime_status as governed_consolidation_status
from .governed_consolidation import validate_proposal_current as validate_governed_consolidation_proposal
from .governed_auto_review import policy_status as governed_auto_review_policy_status
from .governed_auto_review import operator_consent_required as governed_operator_consent_required
from .governed_semantic_editor import GovernedSemanticEditorError
from .governed_semantic_editor import GovernedSemanticEditorUnavailable
from .governed_semantic_editor import LocalGatewaySemanticCompletion
from .governed_semantic_editor import MAX_RETRIEVAL_CANDIDATES
from .governed_semantic_editor import SemanticEditorConfig
from .governed_semantic_editor import build_semantic_proposal
from .governed_semantic_editor import clamp_retrieval_limit
from .governed_semantic_editor import deterministic_no_op
from .governed_semantic_editor import select_authoritative_records
from .governed_semantic_editor import select_consolidatable_records
from .governed_semantic_evaluation import summarize_shadow_proposals
from .memory_doctor import MemoryDoctorSnapshotError
from .memory_doctor import load_authoritative_sqlite_snapshot
from .memory_doctor import run_authoritative_sqlite_memory_doctor
from .runtime_storage import resolve_runtime_storage_config
from .memory_service import MemoryServiceNotReady
from .governed_shared_memory import SharedMemoryRequest
from .governed_shared_memory import SharedOperation
from .governed_shared_memory import SharedVisibility
from .governed_shared_memory import decide_shared_memory
from .shared_memory_audit import append_shared_memory_audit
from .shared_memory_audit import build_shared_memory_audit_event
from .shared_memory_audit import caller_identity_from_principal
from .shared_memory_grants import GRANT_ARTIFACT_TYPE as SHARED_MEMORY_GRANT_ARTIFACT_TYPE
from .shared_memory_grants import REVOCATION_ARTIFACT_TYPE as SHARED_MEMORY_GRANT_REVOCATION_ARTIFACT_TYPE
from .shared_memory_grants import resolve_effective_grants
from .llm_resource_governor import AdmissionRequest
from . import app_core


def _shared_memory_policy_preflight(
    request: SharedMemoryPolicyPreflightRequest,
    *,
    principal: Any,
    auth_kind: str,
) -> dict[str, Any]:
    """Record a governed policy decision without enabling shared-memory I/O."""

    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    project = rt._canonical_project(request.project)
    project_error = authorize_projects(principal, (project,), require_explicit=True)
    if project_error:
        raise HTTPException(status_code=403, detail={"code": project_error})
    try:
        identity = caller_identity_from_principal(principal, project=project)
        policy_request = SharedMemoryRequest(
            request_id=request.request_id,
            operation=request.operation,
            visibility=request.visibility,
            identity=identity,
            owner_id=request.owner_id,
            memory_id=request.memory_id,
            at=request.at,
            sensitivity=request.sensitivity,
            expected_revision=request.expected_revision,
        )
        service = rt._memory_service()
        grants = resolve_effective_grants(
            service.list_artifact_records(
                artifact_type=SHARED_MEMORY_GRANT_ARTIFACT_TYPE, project=project, limit=None,
            ),
            service.list_artifact_records(
                artifact_type=SHARED_MEMORY_GRANT_REVOCATION_ARTIFACT_TYPE, project=project, limit=None,
            ),
            project=project,
        )
        # A grant can influence the preflight result only. No policy result is
        # wired to an actual memory read/write path in this slice.
        receipt = decide_shared_memory(policy_request, grants)
        event = build_shared_memory_audit_event(
            request=policy_request,
            receipt=receipt,
            principal=principal,
            auth_kind=auth_kind,
        )
        _record, inserted = append_shared_memory_audit(service, event)
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "shared_policy_preflight_unavailable", "reason": _safe_exception_text(exc)},
        ) from exc
    return {
        "schema_version": "bhm.governed-shared-memory.preflight.v1",
        "mode": "policy-preflight-only",
        "shared_read_enabled": False,
        "shared_write_enabled": False,
        "decision": receipt.decision.value,
        "reason_code": receipt.reason_code,
        "policy_digest": receipt.policy_digest,
        "audit": {"event_id": event.event_id, "appended": inserted, "authority": "sqlite-authoritative"},
    }


def _serialize_governed_shared_memory(record: dict[str, Any]) -> dict[str, Any]:
    """Return a deliberately bounded subset after a successful policy gate."""

    content = str(record.get("content") or "")
    limit = 16_000
    return {
        "id": str(record.get("source_id") or ""),
        "project": str(record.get("project") or ""),
        "title": str((record.get("metadata") or {}).get("raw_title") or app_core._build_memory_title(content)),
        "type": str(record.get("memory_type") or ""),
        "content": content[:limit],
        "content_truncated": len(content) > limit,
        "source_digest": record.get("source_digest") or (record.get("metadata") or {}).get("source_digest"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "lifecycle": app_core._memory_lifecycle(record),
    }


def _canonical_shared_memory_attributes(record: dict[str, Any]) -> tuple[str, SharedVisibility, str]:
    """Return the declared sharing classification from SQLite or fail closed.

    A caller must never be able to downgrade a record from ``restricted`` to
    ``internal`` or present a private record as ``project`` visible.  Existing
    memories stay private until their owner explicitly records these metadata
    fields; there is intentionally no compatibility default for a shared read.
    """

    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    owner_id = str(record.get("agent_id") or metadata.get("owner_id") or "").strip()
    visibility_value = str(metadata.get("shared_visibility") or "").strip()
    sensitivity = str(metadata.get("sensitivity") or "").strip().casefold()
    if not owner_id:
        raise HTTPException(status_code=409, detail={"code": "shared_memory_owner_unresolved"})
    try:
        visibility = SharedVisibility(visibility_value)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "shared_memory_visibility_unresolved"}) from exc
    if sensitivity not in {"public", "internal", "restricted"}:
        raise HTTPException(status_code=409, detail={"code": "shared_memory_sensitivity_unresolved"})
    return owner_id, visibility, sensitivity


def _shared_memory_read(
    request: SharedMemoryReadRequest,
    *,
    principal: Any,
    auth_kind: str,
) -> dict[str, Any]:
    """Read one active SQLite memory only after an explicit governed ALLOW."""

    if not app_core._shared_memory_read_enabled():
        raise HTTPException(status_code=409, detail={"code": "shared_memory_read_disabled"})
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    project = rt._canonical_project(request.project)
    project_error = authorize_projects(principal, (project,), require_explicit=True)
    if project_error:
        raise HTTPException(status_code=403, detail={"code": project_error})
    try:
        service = rt._memory_service()
        record = rt._find_live_memory(request.memory_id, project)
        if record is None or app_core._memory_lifecycle(record) != "active":
            raise HTTPException(status_code=404, detail={"code": "memory_not_found_in_project"})
        record_owner, record_visibility, record_sensitivity = _canonical_shared_memory_attributes(record)
        if request.owner_id != record_owner:
            raise HTTPException(status_code=403, detail={"code": "shared_memory_owner_mismatch"})
        if request.visibility is not record_visibility:
            raise HTTPException(status_code=403, detail={"code": "shared_memory_visibility_mismatch"})
        if request.sensitivity != record_sensitivity:
            raise HTTPException(status_code=403, detail={"code": "shared_memory_sensitivity_mismatch"})
        identity = caller_identity_from_principal(principal, project=project)
        policy_request = SharedMemoryRequest(
            request_id=request.request_id,
            operation=SharedOperation.READ,
            visibility=request.visibility,
            identity=identity,
            owner_id=request.owner_id,
            memory_id=request.memory_id,
            # Authorization time is server-controlled so a caller cannot use
            # a historical timestamp to bypass an expiry or revocation.
            at=rt._utc_now_iso(),
            sensitivity=record_sensitivity,
            expected_revision=request.expected_revision,
        )
        grants = resolve_effective_grants(
            service.list_artifact_records(
                artifact_type=SHARED_MEMORY_GRANT_ARTIFACT_TYPE, project=project, limit=None,
            ),
            service.list_artifact_records(
                artifact_type=SHARED_MEMORY_GRANT_REVOCATION_ARTIFACT_TYPE, project=project, limit=None,
            ),
            project=project,
        )
        receipt = decide_shared_memory(policy_request, grants)
        # Keep the receipt idempotent on the caller request identity. The
        # supplied timestamp is correlation-only in the audit payload; it has
        # already been excluded from authorization, which uses server time.
        audit_request = policy_request.model_copy(update={"at": request.at})
        event = build_shared_memory_audit_event(
            request=audit_request,
            receipt=receipt,
            principal=principal,
            auth_kind=auth_kind,
        )
        _record, inserted = append_shared_memory_audit(service, event)
    except HTTPException:
        raise
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "shared_memory_read_unavailable", "reason": _safe_exception_text(exc)},
        ) from exc
    if receipt.decision.value != "allow":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "shared_memory_policy_denied",
                "reason_code": receipt.reason_code,
                "audit_event_id": event.event_id,
            },
        )
    return {
        "schema_version": "bhm.governed-shared-memory.read.v1",
        "mode": "governed-read",
        "shared_read_enabled": True,
        "shared_write_enabled": False,
        "decision": receipt.decision.value,
        "reason_code": receipt.reason_code,
        "policy_digest": receipt.policy_digest,
        "memory": _serialize_governed_shared_memory(record),
        "audit": {"event_id": event.event_id, "appended": inserted, "authority": "sqlite-authoritative"},
        "execution": {"sqlite_mutation": bool(inserted), "memory_lifecycle_mutation": False, "qdrant_mutation": False, "mem0_mutation": False},
    }


def _utility_feedback_change_set_preview(
    request: ConsolidationChangeSetPreviewRequest,
) -> dict[str, Any]:
    """Build a snapshot-bound consolidation preview without an apply path."""

    canonical_project = rt._canonical_project(request.project)
    report = app_core._utility_feedback_report(
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


def _require_governed_consolidation_enabled() -> None:
    if not governed_consolidation_enabled():
        raise HTTPException(
            status_code=409,
            detail={"code": "governed_consolidation_disabled", "reason": "BHM_GOVERNED_CONSOLIDATION_ENABLED is not set"},
        )


def _governed_consolidation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GovernedConsolidationMigrationRequired):
        return HTTPException(status_code=409, detail={"code": "governed_consolidation_migration_required"})
    if isinstance(exc, GovernedConsolidationStale):
        return HTTPException(status_code=409, detail={"code": "governed_consolidation_stale"})
    if isinstance(exc, GovernedConsolidationApprovalRequired):
        return HTTPException(status_code=409, detail={"code": "governed_consolidation_approval_required"})
    if isinstance(exc, GovernedSemanticEditorUnavailable):
        return HTTPException(status_code=503, detail={"code": "governed_semantic_editor_unavailable", "reason": _safe_exception_text(exc)})
    if isinstance(exc, GovernedSemanticEditorError):
        return HTTPException(status_code=422, detail={"code": "governed_semantic_editor_invalid", "reason": _safe_exception_text(exc)})
    if isinstance(exc, GovernedConsolidationError):
        return HTTPException(status_code=422, detail={"code": "governed_consolidation_invalid", "reason": _safe_exception_text(exc)})
    return HTTPException(status_code=503, detail={"code": "governed_consolidation_unavailable"})


def _governed_consolidation_project(principal: Any, project: str) -> str:
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    canonical_project = rt._canonical_project(project)
    if error := authorize_projects(principal, (canonical_project,), require_explicit=True):
        raise HTTPException(status_code=403, detail={"code": error})
    return canonical_project


def _governed_consolidation_create(request: GovernedConsolidationCreateRequest, *, principal: Any) -> dict[str, Any]:
    _require_governed_consolidation_enabled()
    project = _governed_consolidation_project(principal, request.project)
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
        stored, inserted = GovernedConsolidationRepository(app_core._governed_consolidation_database_path()).create(proposal)
    except (GovernedConsolidationError, MemoryServiceNotReady, OSError, ValueError) as exc:
        raise _governed_consolidation_error(exc) from exc
    return {
        "proposal": stored,
        "inserted": inserted,
        "replayed": not inserted,
        "side_effects": {"sqlite_mutation": bool(inserted), "memory_lifecycle_mutation": False, "memory_outbox_mutation": False, "qdrant_mutation": False, "mem0_mutation": False},
    }


def _governed_semantic_lexical_fallback_records(
    *,
    project: str,
    query: str,
    limit: int,
    include_historical: bool = False,
) -> list[dict[str, Any]]:
    """Return a bounded same-project SQLite fallback when embeddings are busy.

    This is deliberately a degraded proposal-input path, not a replacement for
    Qdrant/Mem0 retrieval.  It keeps an explicit operator request usable while
    a local embedding model is temporarily unavailable and returns no records
    unless the canonical SQLite text has a deterministic lexical match.
    """

    pool, _excluded = select_consolidatable_records(
        rt._memory_service().list_records(project=project, limit=256),
        include_historical=include_historical,
    )
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for record in pool:
        memory_id = str(record.get("source_id") or record.get("id") or "").strip()
        if not memory_id:
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
        score = app_core._lexical_signal(
            query,
            {
                "memory": record.get("content") or record.get("memory") or "",
                "metadata": {
                    **metadata,
                    "raw_title": record.get("title") or metadata.get("raw_title") or "",
                },
            },
        )
        if score > 0:
            ranked.append((score, memory_id, dict(record)))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = [item[2] for item in ranked[:limit]]
    return select_authoritative_records(
        project=project,
        candidate_ids=[str(item.get("source_id") or item.get("id") or "") for item in selected],
        records=selected,
    ) if selected else []


async def _governed_semantic_proposal(request: GovernedSemanticProposalRequest, *, principal: Any) -> dict[str, Any]:
    """Retrieve projection candidates, re-read SQLite, then emit a safe proposal.

    The vector search only supplies candidate IDs.  The local model sees no
    record until it has been re-read from canonical SQLite and every eventual
    lifecycle mutation remains behind the existing approval/apply route.
    """

    _require_governed_consolidation_enabled()
    project = _governed_consolidation_project(principal, request.project)
    config = SemanticEditorConfig.from_env()
    try:
        requested_limit = clamp_retrieval_limit(request.limit)
        retrieval_source = "federated_semantic_candidates"
        fallback_reason: str | None = None
        excluded_candidate_types: dict[str, int] = {}
        try:
            # Fetch a bounded superset first. Projection ranking legitimately
            # contains workflow artifacts, but they are not model evidence for
            # a consolidation proposal. Filter after SQLite revalidation.
            search_kwargs: dict[str, Any] = {}
            if request.include_historical:
                search_kwargs["include_historical"] = True
            hits, _total = await app_core.federated_search(
                request.query,
                project,
                limit=MAX_RETRIEVAL_CANDIDATES,
                **search_kwargs,
            )
            candidate_ids = [_semantic_graph_node_id(hit) for hit in hits]
            candidate_ids = [item for item in candidate_ids if item]
            records = select_authoritative_records(
                project=project,
                candidate_ids=candidate_ids,
                records=rt._memory_service().get_records(candidate_ids, project=project),
            )
            records, excluded_candidate_types = select_consolidatable_records(
                records,
                include_historical=request.include_historical,
            )
            records = records[:requested_limit]
            if not records:
                retrieval_source = "sqlite_lexical_fallback"
                fallback_reason = "nonconsolidatable_retrieval_candidates"
                records = _governed_semantic_lexical_fallback_records(
                    project=project,
                    query=request.query,
                    limit=requested_limit,
                    include_historical=request.include_historical,
                )
                candidate_ids = [str(item.get("source_id") or item.get("id") or "") for item in records]
        except (app_core.EmbeddingPreparationTimeout, ResponseTimeout, StorageNotReady) as exc:
            # Projection retrieval is an optional proposal input.  A slow or
            # unavailable embedding contour may use a bounded, transparent
            # SQLite lexical fallback. It never treats the fallback as vector
            # authority and fails explicitly when no same-project evidence
            # matches.
            retrieval_source = "sqlite_lexical_fallback"
            fallback_reason = "embedding_retrieval_unavailable"
            records = _governed_semantic_lexical_fallback_records(
                project=project,
                query=request.query,
                limit=requested_limit,
                include_historical=request.include_historical,
            )
            candidate_ids = [str(item.get("source_id") or item.get("id") or "") for item in records]
            if not records:
                raise GovernedSemanticEditorUnavailable(
                    "semantic candidate retrieval is temporarily unavailable"
                ) from exc
        completion = LocalGatewaySemanticCompletion(config)
        job_id = f"governed-semantic-{uuid.uuid4().hex}"
        admission = app_core._llm_governor().admit(
            AdmissionRequest(
                job_id=job_id,
                workload="foreground",
                max_wall_seconds=config.timeout_seconds,
                max_output_tokens=config.max_tokens,
            )
        )
        if not admission.allowed:
            raise GovernedSemanticEditorUnavailable(f"local semantic editor admission denied: {admission.code}")
        try:
            proposal, model_fallback_reason = await _run_governed_semantic_with_retries(
                project=project,
                query=request.query,
                records=records,
                completion=completion,
                retrieval_source=retrieval_source,
                excluded_candidate_types=excluded_candidate_types,
                retire_historical_basis=request.include_historical,
            )
        finally:
            app_core._llm_governor().release(job_id)
        stored: dict[str, Any] | None = None
        inserted = False
        automatic_review: dict[str, Any] | None = None
        if request.store_proposal:
            stored, inserted = GovernedConsolidationRepository(app_core._governed_consolidation_database_path()).create(proposal)
        return {
            "proposal": stored or proposal,
            "stored": request.store_proposal,
            "inserted": inserted,
            "automatic_review": automatic_review,
            "retrieval": {
                "source": retrieval_source,
                "candidate_count": len(candidate_ids),
                "sqlite_revalidated_count": len(records),
                "fallback_reason": fallback_reason,
                "model_fallback_reason": model_fallback_reason,
                "include_historical": request.include_historical,
            },
            "admission": admission.as_dict(),
            "side_effects": {
                "sqlite_mutation": bool(inserted) or bool(automatic_review and automatic_review["side_effects"]["sqlite_mutation"]),
                "memory_lifecycle_mutation": bool(automatic_review and automatic_review["side_effects"]["memory_lifecycle_mutation"]),
                "memory_outbox_mutation": bool(automatic_review and automatic_review["side_effects"]["memory_outbox_mutation"]),
                "qdrant_mutation": False,
                "mem0_mutation": False,
                "automatic_apply": bool(automatic_review and automatic_review["automatic_apply"]),
            },
        }
    except (GovernedConsolidationError, MemoryServiceNotReady, OSError, ValueError) as exc:
        raise _governed_consolidation_error(exc) from exc


GOVERNED_SEMANTIC_RETRY_SCHEMA_VERSION = "bhm.governed-semantic-retry.v1"


GOVERNED_SEMANTIC_RETRY_MAX_ATTEMPTS = 3


def _governed_semantic_retry_limit() -> int:
    """Return an explicit bounded retry count; invalid values fail safe."""

    raw = str(os.getenv("BHM_GOVERNED_SEMANTIC_REVIEW_MAX_ATTEMPTS") or "").strip()
    if not raw:
        return GOVERNED_SEMANTIC_RETRY_MAX_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return GOVERNED_SEMANTIC_RETRY_MAX_ATTEMPTS
    return max(1, min(GOVERNED_SEMANTIC_RETRY_MAX_ATTEMPTS, value))


def _semantic_proposal_candidate_digest(proposal: Mapping[str, Any]) -> str:
    candidate = proposal.get("candidate") if isinstance(proposal.get("candidate"), Mapping) else {}
    payload = {
        "operation": str(proposal.get("operation") or ""),
        "candidate": dict(candidate),
        "confidence": proposal.get("confidence"),
        "conflicts": list(proposal.get("conflicts") or []),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _classify_semantic_retry(proposal: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Classify a model proposal without exposing its content."""

    operation = str(proposal.get("operation") or "")
    conflicts = list(proposal.get("conflicts") or [])
    editor = proposal.get("semantic_editor")
    policy = editor.get("policy") if isinstance(editor, Mapping) else None
    execution = proposal.get("execution")
    if str(proposal.get("analyzer") or "").endswith(":fallback") or (
        isinstance(execution, Mapping) and execution.get("local_model_called") is False
    ):
        return True, "fallback", operation
    if not isinstance(editor, Mapping) or not isinstance(policy, Mapping) or editor.get("shadow_safe") is not True:
        return True, "incomplete_receipt", operation
    if not str(policy.get("decision") or "").strip():
        return True, "incomplete_receipt", operation
    if conflicts:
        return True, "conflict", operation
    try:
        confidence = float(proposal.get("confidence"))
    except (TypeError, ValueError):
        confidence = -1.0
    retry_threshold = {
        "create": 0.90,
        "revise": 0.90,
        "link": 0.90,
        "supersede": 0.97,
        "archive": 0.97,
    }.get(operation)
    if retry_threshold is not None and confidence < retry_threshold:
        return True, "low_confidence", operation
    if operation == "no_op":
        decision = str(policy.get("decision") or "")
        if "confidence" in decision:
            return True, "low_confidence", operation
        return True, "no_op", operation
    return False, "accepted", operation


def _retry_attempt_receipt(*, attempt: int, proposal: Mapping[str, Any], classification: str, operation: str) -> dict[str, Any]:
    try:
        confidence = round(float(proposal.get("confidence")), 6)
    except (TypeError, ValueError):
        confidence = None
    return {
        "attempt": attempt,
        "classification": classification,
        "operation": operation,
        "confidence": confidence,
        "conflict_count": len(list(proposal.get("conflicts") or [])),
        "candidate_digest": _semantic_proposal_candidate_digest(proposal),
    }


def _retry_receipt(*, attempts: list[dict[str, Any]], max_attempts: int, outcome: str, consensus_count: int = 0) -> dict[str, Any]:
    return {
        "schema_version": GOVERNED_SEMANTIC_RETRY_SCHEMA_VERSION,
        "attempt_count": len(attempts),
        "max_attempts": max_attempts,
        "outcome": outcome,
        "consensus_count": consensus_count,
        "attempts": attempts,
    }


async def _run_governed_semantic_with_retries(
    *,
    project: str,
    query: str,
    records: list[dict[str, Any]],
    completion: LocalGatewaySemanticCompletion,
    retrieval_source: str,
    excluded_candidate_types: Mapping[str, int],
    retire_historical_basis: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """Retry only degraded semantic outcomes and require 2-of-3 agreement.

    A clean first result returns immediately. A retryable result receives at
    most three model passes. A lifecycle proposal is returned only when two
    attempts share the same redacted candidate digest; otherwise a no-op is
    emitted and the existing auto-review policy rejects it.
    """

    max_attempts = _governed_semantic_retry_limit()
    attempts: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    candidate_counts: Counter[str] = Counter()
    fallback_reason: str | None = None
    had_retryable = False
    for attempt in range(1, max_attempts + 1):
        try:
            proposal = await asyncio.to_thread(
                build_semantic_proposal,
                project=project,
                query=query,
                retrieved_records=records,
                completion=completion,
                retire_historical_basis=retire_historical_basis,
            )
            fallback_reason = None
            retryable, classification, operation = _classify_semantic_retry(proposal)
            receipt = _retry_attempt_receipt(attempt=attempt, proposal=proposal, classification=classification, operation=operation)
            attempts.append(receipt)
            proposals.append(proposal)
            if not retryable:
                if not had_retryable:
                    proposal["semantic_editor"]["retry"] = _retry_receipt(attempts=attempts, max_attempts=max_attempts, outcome="accepted")
                    proposal["semantic_editor"]["retrieval_source"] = retrieval_source
                    proposal["semantic_editor"]["excluded_candidate_types"] = dict(excluded_candidate_types)
                    proposal["execution"]["semantic_retrieval"] = retrieval_source == "federated_semantic_candidates"
                    proposal["execution"]["local_model_called"] = True
                    return proposal, None
                # Once a degraded pass occurred, one clean answer is not
                # enough: it must agree with a second independent pass.
                key = receipt["candidate_digest"]
                candidate_counts[key] += 1
                if candidate_counts[key] >= 2:
                    winner = next(item for item in proposals if _semantic_proposal_candidate_digest(item) == key)
                    winner["semantic_editor"]["retry"] = _retry_receipt(attempts=attempts, max_attempts=max_attempts, outcome="consensus", consensus_count=candidate_counts[key])
                    winner["semantic_editor"]["retrieval_source"] = retrieval_source
                    winner["semantic_editor"]["excluded_candidate_types"] = dict(excluded_candidate_types)
                    winner["execution"]["semantic_retrieval"] = retrieval_source == "federated_semantic_candidates"
                    winner["execution"]["local_model_called"] = True
                    return winner, None
                if attempt < max_attempts:
                    continue
                break
            key = receipt["candidate_digest"]
            candidate_counts[key] += 1
            had_retryable = True
            if candidate_counts[key] >= 2:
                winner = next(item for item in proposals if _semantic_proposal_candidate_digest(item) == key)
                winner["semantic_editor"]["retry"] = _retry_receipt(attempts=attempts, max_attempts=max_attempts, outcome="consensus", consensus_count=candidate_counts[key])
                winner["semantic_editor"]["retrieval_source"] = retrieval_source
                winner["semantic_editor"]["excluded_candidate_types"] = dict(excluded_candidate_types)
                winner["execution"]["semantic_retrieval"] = retrieval_source == "federated_semantic_candidates"
                winner["execution"]["local_model_called"] = True
                return winner, None
        except (GovernedSemanticEditorUnavailable, GovernedSemanticEditorError) as exc:
            fallback_reason = str(getattr(exc, "code", "local_model_unavailable"))
            had_retryable = True
            attempts.append(
                {
                    "attempt": attempt,
                    "classification": "fallback",
                    "operation": "no_op",
                    "confidence": None,
                    "conflict_count": 0,
                    "candidate_digest": None,
                }
            )
        if attempt < max_attempts:
            continue

    if proposals:
        final = deterministic_no_op(
            project=project,
            query=query,
            retrieved_records=records,
            reason="semantic retries exhausted without a 2-of-3 proposal consensus",
        )
        final["semantic_editor"] = {
            "schema_version": "bhm.governed-semantic-editor.v1",
            "retrieved_candidate_count": len(records),
            "selected_basis_count": len(final.get("basis") or []),
            "policy": {
                "decision": "retry_exhausted_no_consensus",
                "manual_approval_required": True,
                "auto_apply": False,
                "direct_mem0_writes": False,
                "direct_qdrant_writes": False,
            },
            "shadow_safe": True,
            "retry": _retry_receipt(attempts=attempts, max_attempts=max_attempts, outcome="retry_exhausted_no_consensus"),
            "retrieval_source": retrieval_source,
            "excluded_candidate_types": dict(excluded_candidate_types),
        }
        final["execution"]["semantic_retrieval"] = retrieval_source == "federated_semantic_candidates"
        final["execution"]["local_model_called"] = True
        return final, fallback_reason

    proposal = deterministic_no_op(
        project=project,
        query=query,
        retrieved_records=records,
        reason="local semantic editor retries exhausted; deterministic no-op emitted for review",
    )
    proposal["semantic_editor"] = {
        "schema_version": "bhm.governed-semantic-editor.v1",
        "retrieved_candidate_count": len(records),
        "selected_basis_count": len(proposal.get("basis") or []),
        "policy": {
            "decision": "local_model_unavailable_deterministic_no_op",
            "manual_approval_required": True,
            "auto_apply": False,
            "direct_mem0_writes": False,
            "direct_qdrant_writes": False,
        },
        "shadow_safe": True,
        "retry": _retry_receipt(attempts=attempts, max_attempts=max_attempts, outcome="fallback_exhausted"),
        "retrieval_source": retrieval_source,
        "excluded_candidate_types": dict(excluded_candidate_types),
    }
    proposal["execution"]["semantic_retrieval"] = retrieval_source == "federated_semantic_candidates"
    proposal["execution"]["local_model_called"] = False
    return proposal, fallback_reason


def _governed_semantic_shadow_metrics(*, project: str, principal: Any) -> dict[str, Any]:
    """Summarize stored local-editor proposals without reading model prompts."""

    _require_governed_consolidation_enabled()
    canonical_project = _governed_consolidation_project(principal, project)
    try:
        proposals = GovernedConsolidationRepository(app_core._governed_consolidation_database_path()).list(
            project=canonical_project,
            limit=200,
        )
        return {
            "project": canonical_project,
            "metrics": summarize_shadow_proposals(proposals),
            "side_effects": {
                "read_only": True,
                "sqlite_mutation": False,
                "qdrant_mutation": False,
                "mem0_mutation": False,
                "automatic_apply": False,
            },
        }
    except (GovernedConsolidationError, OSError, ValueError) as exc:
        raise _governed_consolidation_error(exc) from exc


def _governed_consolidation_inspect(*, project: str, proposal_id: str, principal: Any) -> dict[str, Any]:
    _require_governed_consolidation_enabled()
    try:
        proposal = GovernedConsolidationRepository(app_core._governed_consolidation_database_path()).get(proposal_id, project=_governed_consolidation_project(principal, project))
    except GovernedConsolidationError as exc:
        raise _governed_consolidation_error(exc) from exc
    return {"proposal": proposal, "side_effects": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def _governed_consolidation_list(*, project: str, status: str | None, limit: int, principal: Any) -> dict[str, Any]:
    _require_governed_consolidation_enabled()
    canonical_project = _governed_consolidation_project(principal, project)
    try:
        proposals = GovernedConsolidationRepository(app_core._governed_consolidation_database_path()).list(project=canonical_project, status=status, limit=limit)
    except GovernedConsolidationError as exc:
        raise _governed_consolidation_error(exc) from exc
    return {"project": canonical_project, "proposals": proposals, "count": len(proposals), "side_effects": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def _governed_consolidation_validate(*, project: str, proposal_id: str, principal: Any) -> dict[str, Any]:
    _require_governed_consolidation_enabled()
    try:
        proposal = GovernedConsolidationRepository(app_core._governed_consolidation_database_path()).get(proposal_id, project=_governed_consolidation_project(principal, project))
        return validate_governed_consolidation_proposal(proposal=proposal, repository=rt._memory_service().repository)
    except (GovernedConsolidationError, MemoryServiceNotReady, OSError, ValueError) as exc:
        raise _governed_consolidation_error(exc) from exc


def _governed_consolidation_dry_run(request: GovernedConsolidationProposalRequest, *, principal: Any) -> dict[str, Any]:
    _require_governed_consolidation_enabled()
    try:
        proposal = GovernedConsolidationRepository(app_core._governed_consolidation_database_path()).get(request.proposal_id, project=_governed_consolidation_project(principal, request.project))
        return dry_run_governed_consolidation_apply(proposal=proposal, repository=rt._memory_service().repository)
    except (GovernedConsolidationError, MemoryServiceNotReady, OSError, ValueError) as exc:
        raise _governed_consolidation_error(exc) from exc


def _governed_consolidation_decide(request: GovernedConsolidationDecisionRequest, *, principal: Any) -> dict[str, Any]:
    _require_governed_consolidation_enabled()
    project = _governed_consolidation_project(principal, request.project)
    try:
        proposal = GovernedConsolidationRepository(app_core._governed_consolidation_database_path()).decide(
            proposal_id=request.proposal_id,
            project=project,
            decision=request.decision,
            actor=str(principal.caller_id),
            semantic_attestation=request.semantic_attestation,
        )
    except GovernedConsolidationError as exc:
        raise _governed_consolidation_error(exc) from exc
    return {"proposal": proposal, "apply_performed": False, "side_effects": {"sqlite_mutation": True, "memory_lifecycle_mutation": False, "memory_outbox_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def _governed_consolidation_apply(request: GovernedConsolidationApplyRequest, *, principal: Any) -> dict[str, Any]:
    _require_governed_consolidation_enabled()
    project = _governed_consolidation_project(principal, request.project)
    try:
        result = apply_approved_proposal(database_path=app_core._governed_consolidation_database_path(), proposal_id=request.proposal_id, project=project, apply=request.apply, confirmation=request.confirmation)
    except (GovernedConsolidationError, OSError, ValueError) as exc:
        raise _governed_consolidation_error(exc) from exc
    return {"proposal_id": result.proposal_id, "status": result.status, "memory_ids": list(result.memory_ids), "outbox_event_ids": list(result.outbox_event_ids), "link_id": result.link_id, "observability": {"apply_duration_ms": result.apply_duration_ms, "outbox": result.outbox}, "side_effects": {"sqlite_mutation": True, "memory_lifecycle_mutation": bool(result.memory_ids), "memory_outbox_mutation": bool(result.outbox_event_ids), "qdrant_mutation": False, "mem0_mutation": False, "projection": "existing_outbox_projector"}}


def _semantic_graph_node_id(item: dict) -> str:
    metadata = item.get("metadata") or {}
    return str(metadata.get("source_id") or item.get("source_id") or item.get("id") or "").strip()


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

    result = governed_consolidation_status(app_core._governed_consolidation_database_path())
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
