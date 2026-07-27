from __future__ import annotations

import pytest

from blackholememory.llm_delegation_policy import DelegationPolicyError
from blackholememory.llm_delegation_policy import decide_delegation
from blackholememory.llm_delegation_policy import delegation_policy_snapshot


def test_local_bounded_workload_is_explainable_and_proposal_only():
    decision = decide_delegation(
        "summarization",
        confidence=0.95,
        local_capabilities=["summarization"],
    )

    assert decision.destination == "local"
    assert "local_bounded_workload" in decision.reason_codes
    assert decision.mutation_allowed is False
    assert decision.as_dict()["auto_apply"] is False


def test_high_risk_and_mutation_workloads_escalate():
    architecture = decide_delegation("architecture", confidence=0.99, local_capabilities=["architecture"])
    destructive = decide_delegation("destructive", confidence=0.99, mutation_requested=True)
    restricted = decide_delegation("summarization", confidence=0.99, sensitivity="restricted")

    assert architecture.destination == "codex"
    assert destructive.destination == "codex"
    assert restricted.destination == "codex"
    assert "mutation_requested" in destructive.reason_codes
    assert "restricted_sensitivity" in restricted.reason_codes


def test_bulk_security_discovery_and_triage_are_local_bounded_workloads():
    for workload in ("security_discovery", "security_triage"):
        decision = decide_delegation(
            workload,
            confidence=0.95,
            sensitivity="internal",
            local_capabilities=["classification", "json", "reasoning"],
            risk_flags=["security"],
        )
        assert decision.destination == "local"
        assert "bounded_security_local_workload" in decision.reason_codes
        assert decision.mutation_allowed is False


def test_security_review_and_secret_bearing_security_stay_codex_owned():
    review = decide_delegation("security_review", confidence=0.95, local_capabilities=["reasoning"], risk_flags=["security"])
    secret = decide_delegation("security_triage", confidence=0.95, local_capabilities=["reasoning"], risk_flags=["security", "secret"])

    assert review.destination == "codex"
    assert secret.destination == "codex"
    assert "codex_owned_workload" in review.reason_codes
    assert "sensitive_risk_flag" in secret.reason_codes


def test_low_confidence_and_missing_capability_escalate():
    low = decide_delegation("candidate_generation", confidence=0.2, local_capabilities=["candidate_generation"])
    missing = decide_delegation("classification", confidence=0.9, local_capabilities=["summarization"])

    assert low.destination == "codex"
    assert "low_confidence_escalation" in low.reason_codes
    assert missing.destination == "codex"
    assert "local_capability_missing" in missing.reason_codes


def test_policy_snapshot_is_stable_and_fail_closed_for_unknown_inputs():
    first = delegation_policy_snapshot()
    second = delegation_policy_snapshot()

    assert first == second
    assert first["mutation_auto_apply"] is False
    assert first["consensus_is_correctness"] is False
    with pytest.raises(DelegationPolicyError):
        decide_delegation("unknown-workload", confidence=0.9)
    with pytest.raises(DelegationPolicyError):
        decide_delegation("summarization", confidence=float("nan"))
