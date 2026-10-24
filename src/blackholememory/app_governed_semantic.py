"""Domain module app_governed_semantic: governed semantic editor proposals and retries.

Moved out of app_governed.py (FIX-017).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections import Counter
from typing import Any, Mapping

from . import app_core_fallback, app_core_hook
from . import app_governed_base as _base
from . import app_runtime as rt
from .api_models import GovernedSemanticProposalRequest
from .app_runtime import ResponseTimeout
from .governed_consolidation import (
    GovernedConsolidationError,
    GovernedConsolidationRepository,
)
from .governed_semantic_editor import (
    MAX_RETRIEVAL_CANDIDATES,
    GovernedSemanticEditorError,
    GovernedSemanticEditorUnavailable,
    LocalGatewaySemanticCompletion,
    SemanticEditorConfig,
    build_semantic_proposal,
    clamp_retrieval_limit,
    deterministic_no_op,
    select_authoritative_records,
    select_consolidatable_records,
)
from .governed_semantic_evaluation import summarize_shadow_proposals
from .llm_resource_governor import AdmissionRequest
from .mem0_adapter import StorageNotReady
from .memory_service import MemoryServiceNotReady


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
        score = app_core_hook._lexical_signal(
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

    _base._require_governed_consolidation_enabled()
    project = _base._governed_consolidation_project(principal, request.project)
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
            hits, _total = await app_core_fallback.federated_search(
                request.query,
                project,
                limit=MAX_RETRIEVAL_CANDIDATES,
                **search_kwargs,
            )
            candidate_ids = [_base._semantic_graph_node_id(hit) for hit in hits]
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
        except (app_core_fallback.EmbeddingPreparationTimeout, ResponseTimeout, StorageNotReady) as exc:
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
        admission = app_core_hook._llm_governor().admit(
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
            app_core_hook._llm_governor().release(job_id)
        stored: dict[str, Any] | None = None
        inserted = False
        automatic_review: dict[str, Any] | None = None
        if request.store_proposal:
            stored, inserted = GovernedConsolidationRepository(app_core_hook._governed_consolidation_database_path()).create(proposal)
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
        raise _base._governed_consolidation_error(exc) from exc


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

    _base._require_governed_consolidation_enabled()
    canonical_project = _base._governed_consolidation_project(principal, project)
    try:
        proposals = GovernedConsolidationRepository(app_core_hook._governed_consolidation_database_path()).list(
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
        raise _base._governed_consolidation_error(exc) from exc
