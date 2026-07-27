from __future__ import annotations

import pytest

from blackholememory.llm_candidates import CandidateBoundsError
from blackholememory.llm_candidates import CandidateError
from blackholememory.llm_candidates import build_candidate_plan
from blackholememory.llm_candidates import evaluate_candidate
from blackholememory.llm_candidates import judge_candidates


def _evidence(*, valid: bool = True, refs: list[str] | None = None) -> dict:
    return {
        "schema_valid": valid,
        "validators": [{"name": "deterministic-check", "passed": valid}],
        "source_refs": refs if refs is not None else ["tests/unit/test_llm_candidates.py"],
        "leakage_free": valid,
        "mutation_free": valid,
        "evidence_digest": "evidence-001" if valid else "",
    }


def test_candidate_plan_is_deterministic_and_role_bounded():
    first = build_candidate_plan("task-179", {"goal": "improve retrieval"})
    second = build_candidate_plan("task-179", {"goal": "improve retrieval"})

    assert first.plan_digest == second.plan_digest
    assert first.plan_id == second.plan_id
    assert [candidate["role"] for candidate in first.candidates] == ["architect", "coder", "tester", "critic"]
    assert all(candidate["independent"] is True for candidate in first.candidates)
    assert first.as_dict()["judge"]["consensus_is_correctness"] is False
    assert first.as_dict()["auto_apply"] is False


def test_candidate_evaluation_requires_evidence_and_returns_proposal_only():
    plan = build_candidate_plan("task-179", {"goal": "improve retrieval"}, roles=["coder"], candidate_count=1)
    candidate = plan.candidates[0]

    accepted = evaluate_candidate(candidate, {"patch": "candidate"}, _evidence())
    rejected = evaluate_candidate(candidate, {"patch": "guess"}, _evidence(valid=False, refs=[]))

    assert accepted["status"] == "validated"
    assert accepted["score"] == 1.0
    assert accepted["proposal"]["authority"] == "proposal"
    assert accepted["proposal"]["auto_apply"] is False
    assert rejected["status"] == "insufficient_evidence"
    assert rejected["score"] == 0.0


def test_judge_prefers_validated_evidence_over_consensus():
    plan = build_candidate_plan("task-179", {"goal": "judge"}, roles=["architect", "coder", "tester"], candidate_count=3)
    valid = evaluate_candidate(plan.candidates[0], {"answer": "validated"}, _evidence())
    invalid_one = evaluate_candidate(plan.candidates[1], {"answer": "popular"}, _evidence(valid=False, refs=[]))
    invalid_two = evaluate_candidate(plan.candidates[2], {"answer": "popular"}, _evidence(valid=False, refs=[]))

    # Force the invalid pair to share an output digest, proving that majority
    # agreement remains an observation rather than a correctness verdict.
    invalid_two["output_digest"] = invalid_one["output_digest"]
    decision = judge_candidates([valid, invalid_one, invalid_two])

    assert decision["winner"]["candidate_id"] == valid["candidate_id"]
    assert decision["correctness"] == "validated_by_evidence"
    assert decision["consensus"][0]["count"] == 2
    assert decision["consensus_is_correctness"] is False
    assert decision["auto_apply"] is False


def test_judge_is_deterministic_for_tied_valid_candidates():
    plan = build_candidate_plan("task-179-tie", {"goal": "tie"}, roles=["architect", "coder"], candidate_count=2)
    results = [
        evaluate_candidate(candidate, {"answer": candidate["role"]}, _evidence())
        for candidate in plan.candidates
    ]

    first = judge_candidates(results)
    second = judge_candidates(list(reversed(results)))

    assert first["judge_digest"] == second["judge_digest"]
    assert first["winner"]["candidate_id"] == second["winner"]["candidate_id"]


def test_candidate_bounds_and_role_errors_fail_closed():
    with pytest.raises(CandidateBoundsError):
        build_candidate_plan("too-many", {"goal": "x"}, candidate_count=9)
    with pytest.raises(CandidateError):
        build_candidate_plan("bad-role", {"goal": "x"}, roles=["oracle"])
