"""Deterministic auto-review for already persisted semantic proposals.

The local model only proposes.  This module owns the separately auditable,
feature-gated policy decision and delegates every actual mutation to the
existing SQLite-authoritative apply path.  It never imports Mem0 or Qdrant.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .governed_consolidation import (
    GovernedConsolidationError,
    GovernedConsolidationRepository,
    validate_proposal_identity,
)
from .governed_semantic_editor import (
    GOVERNED_SEMANTIC_EDITOR_ANALYZER,
    GOVERNED_SEMANTIC_EDITOR_POLICY_VERSION,
    GOVERNED_SEMANTIC_EDITOR_SCHEMA_VERSION,
)

GOVERNED_AUTO_REVIEW_APPLY_POLICY_VERSION = "bhm-governed-auto-review/v1"
GOVERNED_AUTO_REVIEW_APPLY_ACTOR = "bhm-governed-auto-review/v1"
_AUTO_CONFIDENCE_BY_OPERATION = {
    "create": 0.90,
    "revise": 0.90,
    "link": 0.90,
    "supersede": 0.97,
    "archive": 0.97,
}


@dataclass(frozen=True)
class AutoReviewDecision:
    decision: str
    reason_codes: tuple[str, ...]
    confidence_threshold: float | None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def runtime_enabled() -> bool:
    """Return the explicit, default-off opt-in for auto review and apply."""

    return str(os.getenv("BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED") or "").strip().casefold() in {"1", "true", "yes", "on"}


def operator_consent_required() -> bool:
    """Require an explicit launcher/operator action before lifecycle apply."""

    return str(os.getenv("BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED") or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def policy_status() -> dict[str, Any]:
    configured = runtime_enabled()
    consent_required = operator_consent_required()
    return {
        # ``enabled`` retains its existing meaning: the operator enabled the
        # policy adapter.  It is deliberately separate from effective apply.
        "enabled": configured,
        "configured_enabled": configured,
        "mode": "operator-consent" if consent_required else "approval-gated",
        "automatic_apply_enabled": False,
        "automatic_apply_reason": (
            "operator_consent_required"
            if consent_required
            else "semantic_evidence_requires_operator_review"
        ),
        "policy_version": GOVERNED_AUTO_REVIEW_APPLY_POLICY_VERSION,
        "actor": GOVERNED_AUTO_REVIEW_APPLY_ACTOR,
        "minimum_confidence_by_operation": dict(_AUTO_CONFIDENCE_BY_OPERATION),
        "direct_mem0_writes": False,
        "direct_qdrant_writes": False,
        "operator_consent_required": consent_required,
    }


def review_proposal(proposal: Mapping[str, Any], *, allow_operator_consent: bool = False) -> AutoReviewDecision:
    """Return a content-free, deterministic policy decision for one proposal."""

    operation = str(proposal.get("operation") or "")
    reasons: list[str] = []
    if not runtime_enabled() and not allow_operator_consent:
        reasons.append("auto_review_apply_disabled")
    if operation == "no_op":
        reasons.append("no_op")
    elif operation not in _AUTO_CONFIDENCE_BY_OPERATION:
        reasons.append("unsupported_operation")
    if list(proposal.get("conflicts") or []):
        reasons.append("conflicts_present")
    execution = proposal.get("execution") if isinstance(proposal.get("execution"), Mapping) else {}
    if execution.get("local_model_called") is not True:
        reasons.append("local_model_not_confirmed")
    analyzer = str(proposal.get("analyzer") or "")
    if analyzer != GOVERNED_SEMANTIC_EDITOR_ANALYZER:
        reasons.append("semantic_editor_analyzer_required")
    semantic_editor = proposal.get("semantic_editor") if isinstance(proposal.get("semantic_editor"), Mapping) else {}
    if semantic_editor.get("shadow_safe") is not True or semantic_editor.get("schema_version") != GOVERNED_SEMANTIC_EDITOR_SCHEMA_VERSION:
        reasons.append("semantic_editor_receipt_required")
    policy = semantic_editor.get("policy") if isinstance(semantic_editor.get("policy"), Mapping) else None
    if policy is None:
        reasons.append("semantic_policy_receipt_required")
    else:
        model_operation = str(policy.get("model_operation") or "")
        effective_operation = str(policy.get("effective_operation") or "")
        if policy.get("policy_version") != GOVERNED_SEMANTIC_EDITOR_POLICY_VERSION:
            reasons.append("semantic_policy_receipt_stale")
        if model_operation not in _AUTO_CONFIDENCE_BY_OPERATION or effective_operation != operation:
            reasons.append("semantic_policy_receipt_inconsistent")
        if policy.get("manual_approval_required") is not True or policy.get("auto_apply") is not False:
            reasons.append("semantic_policy_receipt_inconsistent")
        if policy.get("direct_mem0_writes") is not False or policy.get("direct_qdrant_writes") is not False:
            reasons.append("semantic_policy_receipt_inconsistent")
        if policy.get("conflicts_present") is not False:
            reasons.append("semantic_policy_conflicts_present")
    legacy_rollover = semantic_editor.get("legacy_rollover") if isinstance(semantic_editor.get("legacy_rollover"), Mapping) else {}
    if legacy_rollover.get("requested") is True:
        reasons.append("historical_rollover_requires_operator_review")
    if analyzer == GOVERNED_SEMANTIC_EDITOR_ANALYZER:
        try:
            validate_proposal_identity(proposal)
        except GovernedConsolidationError:
            # The exact identity covers candidate, basis, target and the policy
            # receipt.  Do not reveal the malformed field through this content-free
            # automatic-review surface.
            reasons.append("semantic_evidence_binding_invalid")
    try:
        confidence = float(proposal.get("confidence"))
    except (TypeError, ValueError):
        confidence = -1.0
    threshold = _AUTO_CONFIDENCE_BY_OPERATION.get(operation)
    if threshold is not None and confidence < threshold:
        reasons.append("confidence_below_auto_threshold")
    return AutoReviewDecision(
        decision="approve" if not reasons else "reject",
        reason_codes=tuple(reasons or ("policy_eligible",)),
        confidence_threshold=threshold,
    )


def auto_review_and_apply_proposal(*, database_path: Path | str, proposal_id: str, project: str) -> dict[str, Any]:
    """Policy-review one persisted proposal and delegate eligible apply safely.

    Repeated calls are idempotent for an already applied proposal.  The apply
    helper revalidates the canonical basis inside the authoritative transaction;
    a race is therefore recorded as ``stale`` rather than producing a second
    mutation.
    """

    store = GovernedConsolidationRepository(database_path)
    proposal = store.get(proposal_id, project=project)
    if not runtime_enabled():
        return _receipt(proposal_id, str(proposal["status"]), decision=None, apply_result=None, idempotent=False)
    if operator_consent_required():
        return _receipt(
            proposal_id,
            str(proposal["status"]),
            decision=None,
            apply_result=None,
            idempotent=False,
            deferred_reason="operator_consent_required",
        )
    if proposal["status"] == "applied":
        return _receipt(proposal_id, "applied", decision=None, apply_result=None, idempotent=True)
    if proposal["status"] == "approved":
        # Approval is durable operator evidence.  Semantic evidence is not yet
        # independently support-mapped, so an internal auto-review call must
        # never turn that evidence into a lifecycle mutation.
        return _receipt(
            proposal_id,
            "approved",
            decision=None,
            apply_result=None,
            idempotent=False,
            deferred_reason="semantic_evidence_requires_operator_review",
        )
    if proposal["status"] != "proposed":
        return _receipt(proposal_id, str(proposal["status"]), decision=None, apply_result=None, idempotent=False)

    decision = review_proposal(proposal)
    if decision.decision == "reject":
        store.decide_automatically(
            proposal_id=proposal_id,
            project=project,
            decision="reject",
            actor=GOVERNED_AUTO_REVIEW_APPLY_ACTOR,
            policy_version=GOVERNED_AUTO_REVIEW_APPLY_POLICY_VERSION,
            reason_codes=decision.reason_codes,
        )
        return _receipt(proposal_id, "rejected", decision=decision, apply_result=None, idempotent=False)

    # A valid receipt only permits an operator review.  Automatic semantic
    # lifecycle apply remains disabled until a separately governed, immutable
    # evidence-support contract exists.
    return _receipt(
        proposal_id,
        "proposed",
        decision=decision,
        apply_result=None,
        idempotent=False,
        deferred_reason="semantic_evidence_requires_operator_review",
    )


def _receipt(
    proposal_id: str,
    status: str,
    *,
    decision: AutoReviewDecision | None,
    apply_result: Any,
    idempotent: bool,
    proposal: Mapping[str, Any] | None = None,
    deferred_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "status": status,
        "automatic_review": decision is not None,
        "automatic_apply": bool(apply_result is not None),
        "idempotent": idempotent,
        "deferred_reason": deferred_reason,
        "policy": policy_status(),
        "decision": decision.as_dict() if decision is not None else None,
        "apply": (
            {
                "memory_ids": list(apply_result.memory_ids),
                "outbox_event_ids": list(apply_result.outbox_event_ids),
                "link_id": apply_result.link_id,
                "apply_duration_ms": apply_result.apply_duration_ms,
                "outbox": apply_result.outbox,
            }
            if apply_result is not None
            else None
        ),
        "approved_status": proposal["status"] if proposal is not None else None,
        "side_effects": {
            "sqlite_mutation": bool(apply_result is not None),
            "memory_lifecycle_mutation": bool(apply_result is not None and apply_result.memory_ids),
            "memory_outbox_mutation": bool(apply_result is not None and apply_result.outbox_event_ids),
            "qdrant_mutation": False,
            "mem0_mutation": False,
        },
    }


__all__ = [
    "GOVERNED_AUTO_REVIEW_APPLY_ACTOR",
    "GOVERNED_AUTO_REVIEW_APPLY_POLICY_VERSION",
    "AutoReviewDecision",
    "auto_review_and_apply_proposal",
    "policy_status",
    "operator_consent_required",
    "review_proposal",
    "runtime_enabled",
]
