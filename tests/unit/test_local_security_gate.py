from __future__ import annotations

import json
from pathlib import Path

from blackholememory.local_security_gate import evaluate_local_security_gate
from blackholememory.local_security_gate import load_json_object


ROOT = Path(__file__).resolve().parents[2]


def _policy(**overrides):
    policy = load_json_object(ROOT / "config" / "security-scan-local-llm.json")
    policy.update(overrides)
    return policy


def _attestation(**overrides):
    value = {
        "schema_version": "bhm.security.local-llm-gate.v1",
        "local_only": True,
        "remote_fallback_detected": False,
        "endpoint": "http://127.0.0.1:57718/v1",
        "model_id": "qwen2.5-coder-7b-instruct",
        "available": True,
        "capabilities": ["classification", "json", "reasoning"],
        "json_parseable": True,
        "tool_schema_accepted": True,
        "provenance": True,
        "deterministic_validation": True,
        "budget_enforced": True,
        "queue_governor": True,
        "bulk_worker_contract_ready": True,
        "proposal_only": True,
        "auto_apply": False,
        "training_enabled": False,
    }
    value.update(overrides)
    return value


def test_checked_in_policy_is_enabled_only_for_attested_local_proposals():
    result = evaluate_local_security_gate(_policy())

    assert result["status"] == "blocked"
    assert result["eligible"] is False
    assert "attestation_required" in result["reasons"]
    assert result["model_started"] is False
    assert result["runtime_flags_changed"] is False


def test_enabled_policy_requires_attestation():
    result = evaluate_local_security_gate(_policy(enabled=True))

    assert result["status"] == "blocked"
    assert "attestation_required" in result["reasons"]


def test_valid_attestation_is_ready_but_remains_proposal_only():
    result = evaluate_local_security_gate(_policy(enabled=True), _attestation())

    assert result["status"] == "ready"
    assert result["eligible"] is True
    assert result["authority"] == "proposal"
    assert result["auto_apply"] is False
    assert result["training_enabled"] is False
    assert result["cloud_fallback"] is False


def test_remote_or_unsafe_attestation_fails_closed():
    remote = evaluate_local_security_gate(_policy(enabled=True), _attestation(endpoint="https://example.invalid/v1"))
    unsafe = evaluate_local_security_gate(_policy(enabled=True), _attestation(auto_apply=True))

    assert remote["status"] == "blocked"
    assert "attestation_endpoint_local" in remote["reasons"]
    assert unsafe["status"] == "blocked"
    assert "attestation_auto_apply_disabled" in unsafe["reasons"]


def test_missing_capability_and_worker_contract_fail_closed():
    result = evaluate_local_security_gate(
        _policy(enabled=True),
        _attestation(capabilities=["classification"], bulk_worker_contract_ready=False),
    )

    assert result["status"] == "blocked"
    assert "attestation_capabilities" in result["reasons"]
    assert "attestation_worker_contract" in result["reasons"]


def test_policy_digest_is_deterministic():
    first = evaluate_local_security_gate(_policy())
    second = evaluate_local_security_gate(json.loads(json.dumps(_policy())))

    assert first["policy_digest"] == second["policy_digest"]
