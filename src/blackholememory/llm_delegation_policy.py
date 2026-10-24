"""Explainable local-first delegation policy for the local-LLM coprocessor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


LLM_DELEGATION_POLICY_VERSION = "bhm.llm.delegation-policy.v1"
LOCAL_CONFIDENCE_THRESHOLD = 0.72
LOCAL_EVIDENCE_THRESHOLD = 1
LOCAL_SECURITY_WORKLOADS = ("security_discovery", "security_triage")
LOCAL_WORKLOADS = (
    "bulk_read",
    "summarization",
    "classification",
    "query_expansion",
    "candidate_generation",
    "test_brainstorming",
    "docs_draft",
    *LOCAL_SECURITY_WORKLOADS,
)
CODEX_WORKLOADS = ("architecture", "security_review", "final_integration")
OPERATOR_WORKLOADS = ("destructive", "release", "credential_rotation")
SENSITIVITY_LEVELS = ("public", "internal", "restricted")


class DelegationPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class DelegationDecision:
    destination: str
    task_type: str
    confidence: float
    reason_codes: tuple[str, ...]
    evidence_required: bool
    approval_required: bool
    mutation_allowed: bool
    policy_version: str = LLM_DELEGATION_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.policy_version,
            "destination": self.destination,
            "task_type": self.task_type,
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
            "evidence_required": self.evidence_required,
            "approval_required": self.approval_required,
            "mutation_allowed": self.mutation_allowed,
            "authority": "proposal",
            "auto_apply": False,
            "requires_validation": True,
        }


def decide_delegation(
    task_type: str,
    *,
    confidence: float,
    sensitivity: str = "internal",
    mutation_requested: bool = False,
    evidence_count: int = 0,
    local_capabilities: Sequence[str] | None = None,
    risk_flags: Sequence[str] | None = None,
    operator_approved: bool = False,
) -> DelegationDecision:
    """Return a deterministic destination and escalation reasons."""

    normalized_task = _normalize_task(task_type)
    normalized_sensitivity = str(sensitivity or "internal").strip().casefold()
    if normalized_sensitivity not in SENSITIVITY_LEVELS:
        raise DelegationPolicyError(f"unsupported sensitivity: {sensitivity}")
    score = _bounded_confidence(confidence)
    evidence = max(int(evidence_count), 0)
    capabilities = {str(value or "").strip().casefold() for value in (local_capabilities or []) if str(value or "").strip()}
    risks = {str(value or "").strip().casefold() for value in (risk_flags or []) if str(value or "").strip()}
    reasons: list[str] = []
    destination = "local"
    evidence_required = normalized_task not in {"bulk_read", "summarization"}
    approval_required = False

    if normalized_task in OPERATOR_WORKLOADS:
        destination = "operator"
        reasons.append("operator_only_workload")
        approval_required = True
    elif normalized_task in CODEX_WORKLOADS:
        destination = "codex"
        reasons.append("codex_owned_workload")
        approval_required = True
    if normalized_sensitivity == "restricted":
        destination = "operator" if normalized_task in OPERATOR_WORKLOADS else "codex"
        reasons.append("restricted_sensitivity")
        approval_required = True
    if risks & {"secret", "credential", "security", "destructive", "release"}:
        bounded_local_security = normalized_task in LOCAL_SECURITY_WORKLOADS and risks <= {"security"} and not mutation_requested
        if bounded_local_security and destination == "local":
            reasons.append("bounded_security_local_workload")
        else:
            destination = "operator" if risks & {"destructive", "release", "credential"} else "codex"
            reasons.append("sensitive_risk_flag")
            approval_required = True
    if mutation_requested:
        destination = "operator" if operator_approved else "codex"
        reasons.append("mutation_requested")
        approval_required = True
    if score < LOCAL_CONFIDENCE_THRESHOLD:
        if destination == "local":
            destination = "codex"
        reasons.append("low_confidence_escalation")
        approval_required = True
    if normalized_task in LOCAL_WORKLOADS and normalized_task not in LOCAL_SECURITY_WORKLOADS and normalized_task not in capabilities and capabilities:
        if destination == "local":
            destination = "codex"
        reasons.append("local_capability_missing")
        approval_required = True
    if evidence_required and evidence < LOCAL_EVIDENCE_THRESHOLD:
        reasons.append("evidence_required_before_acceptance")
    if not reasons:
        reasons.append("local_bounded_workload")
    if destination == "local":
        reasons.append("proposal_only_local_execution")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return DelegationDecision(
        destination=destination,
        task_type=normalized_task,
        confidence=score,
        reason_codes=unique_reasons,
        evidence_required=evidence_required,
        approval_required=approval_required,
        mutation_allowed=False,
    )


def delegation_policy_snapshot() -> dict[str, Any]:
    return {
        "schema_version": LLM_DELEGATION_POLICY_VERSION,
        "local_workloads": list(LOCAL_WORKLOADS),
        "local_security_workloads": list(LOCAL_SECURITY_WORKLOADS),
        "codex_workloads": list(CODEX_WORKLOADS),
        "operator_workloads": list(OPERATOR_WORKLOADS),
        "sensitivity_levels": list(SENSITIVITY_LEVELS),
        "confidence_threshold": LOCAL_CONFIDENCE_THRESHOLD,
        "evidence_threshold": LOCAL_EVIDENCE_THRESHOLD,
        "low_confidence_escalates": True,
        "restricted_escalates": True,
        "mutation_auto_apply": False,
        "consensus_is_correctness": False,
        "authority": "proposal",
        "execution_enabled": False,
    }


def _normalize_task(task_type: str) -> str:
    normalized = str(task_type or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "bulk": "bulk_read",
        "read": "bulk_read",
        "summary": "summarization",
        "summarize": "summarization",
        "classify": "classification",
        "query_rewrite": "query_expansion",
        "candidate": "candidate_generation",
        "test_brainstorm": "test_brainstorming",
        "docs": "docs_draft",
        "security": "security_review",
        "final": "final_integration",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {*LOCAL_WORKLOADS, *CODEX_WORKLOADS, *OPERATOR_WORKLOADS}:
        raise DelegationPolicyError(f"unsupported task type: {task_type}")
    return normalized


def _bounded_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise DelegationPolicyError("confidence must be numeric") from None
    if number != number or number in {float("inf"), float("-inf")}:
        raise DelegationPolicyError("confidence must be finite")
    return round(min(max(number, 0.0), 1.0), 6)


__all__ = [
    "CODEX_WORKLOADS",
    "DelegationDecision",
    "DelegationPolicyError",
    "LLM_DELEGATION_POLICY_VERSION",
    "LOCAL_WORKLOADS",
    "LOCAL_SECURITY_WORKLOADS",
    "OPERATOR_WORKLOADS",
    "decide_delegation",
    "delegation_policy_snapshot",
]
