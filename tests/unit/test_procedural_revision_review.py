from __future__ import annotations

import hashlib
import json

import pytest

from blackholememory.memory_contracts import ProcedureExecutionTraceReceipt
from blackholememory.memory_contracts import ProcedureOutcomeReceipt
from blackholememory.consolidation_change_set import build_consolidation_change_set_preview
from blackholememory.utility_feedback import UtilityEvent
from blackholememory.utility_feedback import utility_report
from blackholememory.procedural_revision_review import ProceduralRevisionReviewError
from blackholememory.procedural_revision_review import build_procedural_revision_consolidation_candidate
from blackholememory.procedural_revision_review import build_procedural_revision_review


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _trace(execution_id: str, *, failed: bool = False) -> ProcedureExecutionTraceReceipt:
    return ProcedureExecutionTraceReceipt.model_validate({
        "execution_id": execution_id,
        "project": "blackholememory",
        "memory_id": "memory-procedure",
        "procedure_version": "2",
        "procedure_digest": _digest("procedure-v2"),
        "approval_receipt_digest": _digest("approval"),
        "status": "failed" if failed else "succeeded",
        "started_at": "2026-08-23T12:00:00Z",
        "completed_at": "2026-08-23T12:01:00Z",
        "steps": [{
            "step_id": "validate-input",
            "status": "failed" if failed else "succeeded",
            "started_at": "2026-08-23T12:00:00Z",
            "completed_at": "2026-08-23T12:01:00Z",
            "error_code": "validation_failed" if failed else None,
        }],
    })


def _outcome(outcome_id: str, trace: ProcedureExecutionTraceReceipt, *, failed: bool = False, previous: str | None = None) -> ProcedureOutcomeReceipt:
    return ProcedureOutcomeReceipt.model_validate({
        "outcome_id": outcome_id,
        "trace_receipt_digest": trace.receipt_digest,
        "project": "blackholememory",
        "memory_id": "memory-procedure",
        "procedure_version": "2",
        "procedure_digest": _digest("procedure-v2"),
        "previous_procedure_digest": previous,
        "status": "failed" if failed else "succeeded",
        "observed_at": f"2026-08-23T12:0{outcome_id[-1]}:00Z",
        "failed_step_id": "validate-input" if failed else None,
        "violated_assumption_ids": ["input.schema"] if failed else [],
        "derived_precondition_ids": ["require.input-schema"] if failed else [],
    })


def _reference() -> dict[str, object]:
    return {
        "memory_id": "memory-procedure",
        "revision_id": "revision-current",
        "content_sha256": _digest("memory"),
        "lifecycle": "active",
        "authority_seq": 7,
    }


def test_review_aggregates_bound_outcomes_deterministically_and_is_non_executable() -> None:
    success = _trace("execution-success")
    failed_one, failed_two = _trace("execution-failure-one", failed=True), _trace("execution-failure-two", failed=True)
    previous = _digest("procedure-v1")
    outcomes = (
        _outcome("outcome-1", success, previous=previous),
        _outcome("outcome-2", failed_one, failed=True, previous=previous),
        _outcome("outcome-3", failed_two, failed=True, previous=previous),
    )

    one = build_procedural_revision_review((success, failed_one, failed_two), outcomes, as_of="2026-08-23T13:00:00Z")
    two = build_procedural_revision_review(tuple(reversed((success, failed_one, failed_two))), tuple(reversed(outcomes)), as_of="2026-08-23T13:00:00Z")

    assert one == two
    review = one["reviews"][0]
    assert review["success_count"] == 1
    assert review["failure_count"] == 2
    assert review["failed_step_counts"] == {"validate-input": 2}
    assert review["violated_assumption_ids"] == ("input.schema",)
    assert review["derived_precondition_ids"] == ("require.input-schema",)
    assert review["successful_trace_candidate_count"] == 1
    assert review["successful_trace_authority"] == "review_required"
    assert review["revision_candidate"] is True
    assert one["execution"] == {
        "read_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "procedure_executed": False,
        "procedure_revised": False,
        "automatic_lifecycle_action": False,
    }
    assert "validation_failed" not in json.dumps(one)


def test_failure_candidate_requires_pattern_previous_link_and_still_needs_change_set_gates() -> None:
    failed_one, failed_two = _trace("execution-failure-one", failed=True), _trace("execution-failure-two", failed=True)
    without_link = build_procedural_revision_review(
        (failed_one, failed_two),
        (_outcome("outcome-1", failed_one, failed=True), _outcome("outcome-2", failed_two, failed=True)),
        as_of="2026-08-23T13:00:00Z",
    )["reviews"][0]
    assert without_link["revision_candidate"] is False
    assert build_procedural_revision_consolidation_candidate(without_link, memory_reference=_reference()) is None

    previous = _digest("procedure-v1")
    with_link = build_procedural_revision_review(
        (failed_one, failed_two),
        (_outcome("outcome-1", failed_one, failed=True, previous=previous), _outcome("outcome-2", failed_two, failed=True, previous=previous)),
        as_of="2026-08-23T13:00:00Z",
    )["reviews"][0]
    candidate = build_procedural_revision_consolidation_candidate(with_link, memory_reference=_reference())
    assert candidate == {
        "project": "blackholememory",
        "kind": "procedural_revision_review",
        "memory_refs": [_reference()],
        "reason_codes": ["procedural_failure_pattern"],
        "detector_digest": candidate["detector_digest"],
        "confidence": 0.5,
    }


def test_failure_candidate_remains_review_only_inside_the_existing_change_set_gate() -> None:
    first, second = _trace("execution-failure-one", failed=True), _trace("execution-failure-two", failed=True)
    previous = _digest("procedure-v1")
    review = build_procedural_revision_review(
        (first, second),
        (_outcome("outcome-1", first, failed=True, previous=previous), _outcome("outcome-2", second, failed=True, previous=previous)),
        as_of="2026-08-23T13:00:00Z",
    )["reviews"][0]
    candidate = build_procedural_revision_consolidation_candidate(review, memory_reference=_reference())
    assert candidate is not None
    record = {
        "memory_id": "memory-procedure", "project": "blackholememory", "content_digest": _digest("memory"),
        "lifecycle": "active", "revision_id": "revision-current", "source_digest": "", "schema_digest": "",
        "authority_seq": 7, "projection_seq": 7, "supersedes_revision_id": "", "ontology_schema_digest": "",
        "shared_visibility": "", "shared_owner_digest": "", "sensitivity": "",
    }
    snapshot = {"schema_version": "bhm.memory-doctor.sqlite-snapshot.v1", "records": [record]}
    snapshot["snapshot_digest"] = hashlib.sha256(json.dumps(snapshot["records"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    doctor = {
        "schema_version": "bhm.memory-doctor.v1", "authority_snapshot": {"snapshot_digest": snapshot["snapshot_digest"]},
        "findings": [{"reason_code": "procedural_failure_pattern", "memory_ids": ["memory-procedure"]}], "execution": {"read_only": True},
    }
    doctor["report_digest"] = hashlib.sha256(json.dumps(doctor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    events = tuple(
        UtilityEvent(
            event_id=f"event-{index}", memory_id="memory-procedure", project="blackholememory", actor_id=f"actor-{index % 2}",
            event_type="contradicted", observed_at="2026-08-23T12:00:00Z", request_digest=_digest(f"request-{index}"),
        )
        for index in range(3)
    )
    result = build_consolidation_change_set_preview(
        utility_report(events, as_of="2026-08-23T13:00:00Z"), project="blackholememory", authority_snapshot=snapshot,
        candidates=[candidate], doctor_report=doctor, as_of="2026-08-23T13:00:00Z",
    )
    assert result["actions"][0]["kind"] == "procedural_revision_review"
    assert result["actions"][0]["lifecycle_action"] == "none"
    assert result["execution"]["apply_performed"] is False


def test_review_fails_closed_for_unbound_or_inconsistent_failure_evidence() -> None:
    trace = _trace("execution-failure", failed=True)
    outcome = _outcome("outcome-1", trace, failed=True, previous=_digest("procedure-v1"))
    with pytest.raises(ProceduralRevisionReviewError, match="trace is missing"):
        build_procedural_revision_review((), (outcome,), as_of="2026-08-23T13:00:00Z")

    invalid = outcome.model_copy(update={"failed_step_id": "other-step", "outcome_digest": None})
    with pytest.raises(ProceduralRevisionReviewError, match="step is not backed"):
        build_procedural_revision_review((trace,), (invalid,), as_of="2026-08-23T13:00:00Z")

    collision = outcome.model_copy(update={"status": "succeeded", "failed_step_id": None, "violated_assumption_ids": (), "derived_precondition_ids": (), "outcome_digest": None})
    with pytest.raises(ProceduralRevisionReviewError, match="id collision"):
        build_procedural_revision_review((trace,), (outcome, collision), as_of="2026-08-23T13:00:00Z")

    tampered = outcome.model_copy(update={"failed_step_id": "other-step"})
    with pytest.raises(ProceduralRevisionReviewError, match="outcome receipt is invalid"):
        build_procedural_revision_review((trace,), (tampered,), as_of="2026-08-23T13:00:00Z")
