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

from .governed_consolidation import GovernedConsolidationError
from .governed_consolidation import GovernedConsolidationRepository
from .governed_consolidation import GovernedConsolidationStale
from .governed_consolidation import apply_approved_proposal
from .governed_semantic_editor import GOVERNED_SEMANTIC_EDITOR_ANALYZER


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
    return {
        "enabled": runtime_enabled(),
        "mode": "operator-consent" if operator_consent_required() else ("policy-auto-reviewed" if runtime_enabled() else "approval-gated"),
        "policy_version": GOVERNED_AUTO_REVIEW_APPLY_POLICY_VERSION,
        "actor": GOVERNED_AUTO_REVIEW_APPLY_ACTOR,
        "minimum_confidence_by_operation": dict(_AUTO_CONFIDENCE_BY_OPERATION),
        "direct_mem0_writes": False,
        "direct_qdrant_writes": False,
        "operator_consent_required": operator_consent_required(),
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
    if semantic_editor.get("shadow_safe") is not True:
        reasons.append("semantic_editor_receipt_required")
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
        try:
            result = apply_approved_proposal(
                database_path=database_path,
                proposal_id=proposal_id,
                project=project,
                apply=True,
                confirmation=proposal_id,
            )
        except GovernedConsolidationStale:
            current = store.get(proposal_id, project=project)
            if current["status"] == "applied":
                return _receipt(proposal_id, "applied", decision=None, apply_result=None, idempotent=True)
            return _receipt(proposal_id, "stale", decision=None, apply_result=None, idempotent=False)
        return _receipt(proposal_id, result.status, decision=None, apply_result=result, idempotent=False)
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

    approved = store.decide_automatically(
        proposal_id=proposal_id,
        project=project,
        decision="approve",
        actor=GOVERNED_AUTO_REVIEW_APPLY_ACTOR,
        policy_version=GOVERNED_AUTO_REVIEW_APPLY_POLICY_VERSION,
        reason_codes=decision.reason_codes,
    )
    try:
        result = apply_approved_proposal(
            database_path=database_path,
            proposal_id=proposal_id,
            project=project,
            apply=True,
            confirmation=proposal_id,
        )
    except GovernedConsolidationStale:
        return _receipt(proposal_id, "stale", decision=decision, apply_result=None, idempotent=False)
    except GovernedConsolidationError:
        # The stored status/event remains evidence of the policy decision. The
        # caller receives the original controlled error from the canonical path.
        raise
    return _receipt(proposal_id, result.status, decision=decision, apply_result=result, idempotent=False, proposal=approved)


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
