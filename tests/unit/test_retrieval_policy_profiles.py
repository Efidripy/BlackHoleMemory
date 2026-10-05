from __future__ import annotations

import pytest

from blackholememory.retrieval_policy_profiles import RetrievalPolicyCandidate, RetrievalPolicyError, RetrievalPolicyProfile, build_policy_canary_receipt, build_retrieval_policy_preview, verify_retrieval_policy_preview


def _profile(**changes):
    values = {"profile_id": "controlled-fusion", "version": "v1", "weights": {"lexical": 0.4, "vector": 0.3, "entity": 0.1, "recency": 0.1, "provenance": 0.1}}
    values.update(changes)
    return RetrievalPolicyProfile.model_validate(values)


def _candidate(candidate_id: str, **scores):
    return RetrievalPolicyCandidate(candidate_id=candidate_id, project="blackholememory", scores={"lexical": 0.0, "vector": 0.0, "entity": 0.0, "recency": 0.0, "provenance": 0.0, **scores})


def test_preview_is_default_off_deterministic_and_explains_all_contributions():
    profile = _profile()
    candidates = (_candidate("a", lexical=1, vector=0.5), _candidate("b", vector=1, provenance=1))
    first = build_retrieval_policy_preview(profile, candidates, project="blackholememory")
    assert first == build_retrieval_policy_preview(profile, tuple(reversed(candidates)), project="blackholememory")
    assert first["ranked"][0]["candidate_id"] == "a"
    assert first["ranked"][0]["contributions"] == {"lexical": 0.4, "vector": 0.15, "entity": 0.0, "recency": 0.0, "provenance": 0.0}
    assert first["execution"]["live_retrieval_changed"] is False
    assert verify_retrieval_policy_preview(first)


def test_profiles_and_candidates_fail_closed_on_default_enable_raw_or_cross_scope_input():
    with pytest.raises(ValueError, match="default-off"):
        _profile(default_enabled=True)
    with pytest.raises(ValueError, match="candidate_id"):
        _candidate("Bearer-secret-token", lexical=1)
    with pytest.raises(ValueError, match="outside bounds"):
        _candidate("finite-only", lexical=float("nan"))
    with pytest.raises(RetrievalPolicyError, match="project mismatch"):
        build_retrieval_policy_preview(_profile(), (_candidate("a"),), project="other-project")


def test_tamper_and_canary_latency_failure_cannot_activate_policy():
    preview = build_retrieval_policy_preview(_profile(), (_candidate("a", lexical=1),), project="blackholememory")
    tampered = {**preview, "ranked": []}
    assert not verify_retrieval_policy_preview(tampered)
    with pytest.raises(RetrievalPolicyError, match="valid preview"):
        build_policy_canary_receipt(tampered, preview, p95_latency_ms=1, max_p95_latency_ms=2)
    receipt = build_policy_canary_receipt(preview, preview, p95_latency_ms=25, max_p95_latency_ms=20)
    assert receipt["canary_passed"] is False
    assert receipt["execution"] == {"canary_only": True, "activation_performed": False, "rollback": "retain-current-policy"}
