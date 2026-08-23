"""Content-free, review-only procedural outcome aggregation for WL-300.6.

The module receives already-recorded trace and outcome receipts.  It does not
open storage, alter a procedure, or decide that an observed successful trace
is authoritative.  Its only output is a deterministic candidate that an
operator may later review through the existing consolidation change-set gate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from blackholememory.consolidation_change_set import MemoryReference
from blackholememory.memory_contracts import ProcedureExecutionTraceReceipt
from blackholememory.memory_contracts import ProcedureOutcomeReceipt
from blackholememory.memory_contracts import ProcedureStepStatus
from blackholememory.memory_contracts import ProcedureTraceStatus


PROCEDURAL_REVISION_REVIEW_SCHEMA_VERSION = "bhm.procedure-revision-review.v1"
MIN_FAILURE_PATTERN = 2
MAX_RECEIPTS = 512


class ProceduralRevisionReviewError(ValueError):
    """Raised when an outcome cannot be proven against its trace."""


def build_procedural_revision_review(
    traces: Sequence[ProcedureExecutionTraceReceipt],
    outcomes: Sequence[ProcedureOutcomeReceipt],
    *,
    as_of: str,
) -> dict[str, Any]:
    """Build deterministic, non-executable outcome aggregates.

    Each outcome is bound to exactly one supplied trace.  Failure candidates
    require two independently recorded failures and an explicit previous
    procedure digest; a successful trace only contributes a review candidate
    count and can never promote or replace the current contract.
    """

    if _invalid_sequence(traces) or _invalid_sequence(outcomes):
        raise ProceduralRevisionReviewError("traces and outcomes must be bounded arrays")
    if len(traces) > MAX_RECEIPTS or len(outcomes) > MAX_RECEIPTS:
        raise ProceduralRevisionReviewError(f"traces and outcomes are limited to {MAX_RECEIPTS}")

    trace_by_digest: dict[str, ProcedureExecutionTraceReceipt] = {}
    for raw_trace in traces:
        trace = _revalidate_trace(raw_trace)
        existing = trace_by_digest.get(str(trace.receipt_digest))
        if existing is not None and existing != trace:
            raise ProceduralRevisionReviewError("procedure trace digest collision")
        trace_by_digest[str(trace.receipt_digest)] = trace

    seen_outcomes: dict[str, ProcedureOutcomeReceipt] = {}
    seen_traces: set[str] = set()
    grouped: dict[tuple[str, str, str, str, str | None], list[ProcedureOutcomeReceipt]] = {}
    for raw_outcome in outcomes:
        outcome = _revalidate_outcome(raw_outcome)
        existing = seen_outcomes.get(outcome.outcome_id)
        if existing is not None and existing != outcome:
            raise ProceduralRevisionReviewError("procedure outcome id collision")
        seen_outcomes[outcome.outcome_id] = outcome
        trace = trace_by_digest.get(outcome.trace_receipt_digest)
        if trace is None:
            raise ProceduralRevisionReviewError("procedure outcome trace is missing")
        if outcome.trace_receipt_digest in seen_traces:
            raise ProceduralRevisionReviewError("procedure trace may have only one outcome receipt")
        seen_traces.add(outcome.trace_receipt_digest)
        _validate_outcome_against_trace(outcome, trace)
        grouped.setdefault(
            (outcome.project, outcome.memory_id, outcome.procedure_version, outcome.procedure_digest, outcome.previous_procedure_digest),
            [],
        ).append(outcome)

    reviews = [_review_row(key, receipts) for key, receipts in sorted(grouped.items())]
    core = {
        "schema_version": PROCEDURAL_REVISION_REVIEW_SCHEMA_VERSION,
        "as_of": _bounded_text(as_of, "as_of"),
        "review_count": len(reviews),
        "reviews": reviews,
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "procedure_executed": False,
            "procedure_revised": False,
            "automatic_lifecycle_action": False,
        },
    }
    return {**core, "report_digest": _digest(core)}


def build_procedural_revision_consolidation_candidate(
    review: Mapping[str, Any],
    *,
    memory_reference: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Convert an eligible aggregate into the existing review-only candidate.

    ``None`` is returned for a non-pattern/no-previous-version aggregate.  It
    is deliberately not an apply request and still needs the utility and
    snapshot gates in ``build_consolidation_change_set_preview``.
    """

    _verify_review_digest(review)
    reference = MemoryReference.model_validate(memory_reference)
    if reference.memory_id != str(review.get("memory_id") or ""):
        raise ProceduralRevisionReviewError("memory reference does not match procedural review")
    if int(review.get("failure_count") or 0) < MIN_FAILURE_PATTERN or not review.get("previous_procedure_digest"):
        return None
    canonical = {
        "review_digest": str(review["review_digest"]),
        "project": str(review["project"]),
        "memory_id": reference.memory_id,
        "procedure_digest": str(review["procedure_digest"]),
        "previous_procedure_digest": str(review["previous_procedure_digest"]),
        "failed_step_counts": review["failed_step_counts"],
        "violated_assumption_ids": review["violated_assumption_ids"],
        "derived_precondition_ids": review["derived_precondition_ids"],
    }
    return {
        "project": str(review["project"]),
        "kind": "procedural_revision_review",
        "memory_refs": [reference.model_dump(mode="json")],
        "reason_codes": ["procedural_failure_pattern"],
        "detector_digest": _digest(canonical),
        "confidence": 0.5,
    }


def _validate_outcome_against_trace(outcome: ProcedureOutcomeReceipt, trace: ProcedureExecutionTraceReceipt) -> None:
    if (
        outcome.project != trace.project
        or outcome.memory_id != trace.memory_id
        or outcome.procedure_version != trace.procedure_version
        or outcome.procedure_digest != trace.procedure_digest
    ):
        raise ProceduralRevisionReviewError("procedure outcome does not match trace identity")
    if outcome.status == "succeeded":
        if trace.status is not ProcedureTraceStatus.SUCCEEDED or any(step.status is ProcedureStepStatus.FAILED for step in trace.steps):
            raise ProceduralRevisionReviewError("successful outcome is not backed by a successful trace")
        return
    if trace.status is not ProcedureTraceStatus.FAILED:
        raise ProceduralRevisionReviewError("failed outcome is not backed by a failed trace")
    if not any(step.step_id == outcome.failed_step_id and step.status is ProcedureStepStatus.FAILED for step in trace.steps):
        raise ProceduralRevisionReviewError("failed outcome step is not backed by the trace")


def _revalidate_trace(value: ProcedureExecutionTraceReceipt) -> ProcedureExecutionTraceReceipt:
    try:
        return ProcedureExecutionTraceReceipt.model_validate(value.model_dump(mode="json"))
    except Exception as exc:
        raise ProceduralRevisionReviewError("procedure trace receipt is invalid") from exc


def _revalidate_outcome(value: ProcedureOutcomeReceipt) -> ProcedureOutcomeReceipt:
    try:
        return ProcedureOutcomeReceipt.model_validate(value.model_dump(mode="json"))
    except Exception as exc:
        raise ProceduralRevisionReviewError("procedure outcome receipt is invalid") from exc


def _review_row(
    key: tuple[str, str, str, str, str | None], receipts: list[ProcedureOutcomeReceipt]
) -> dict[str, Any]:
    project, memory_id, procedure_version, procedure_digest, previous_procedure_digest = key
    ordered = sorted(receipts, key=lambda item: (item.observed_at, item.outcome_id))
    failures = [item for item in ordered if item.status == "failed"]
    failed_step_counts: dict[str, int] = {}
    for item in failures:
        assert item.failed_step_id is not None
        failed_step_counts[item.failed_step_id] = failed_step_counts.get(item.failed_step_id, 0) + 1
    core = {
        "project": project,
        "memory_id": memory_id,
        "procedure_version": procedure_version,
        "procedure_digest": procedure_digest,
        "previous_procedure_digest": previous_procedure_digest,
        "outcome_count": len(ordered),
        "success_count": len(ordered) - len(failures),
        "failure_count": len(failures),
        "failed_step_counts": dict(sorted(failed_step_counts.items())),
        "violated_assumption_ids": tuple(sorted({value for item in failures for value in item.violated_assumption_ids})),
        "derived_precondition_ids": tuple(sorted({value for item in failures for value in item.derived_precondition_ids})),
        "successful_trace_candidate_count": len(ordered) - len(failures),
        "successful_trace_authority": "review_required",
        "revision_candidate": bool(len(failures) >= MIN_FAILURE_PATTERN and previous_procedure_digest),
        "status": "operator_review_required",
        "lifecycle_action": "none",
    }
    return {**core, "review_digest": _digest(core)}


def _verify_review_digest(review: Mapping[str, Any]) -> None:
    required = {
        "project", "memory_id", "procedure_version", "procedure_digest", "previous_procedure_digest", "outcome_count",
        "success_count", "failure_count", "failed_step_counts", "violated_assumption_ids", "derived_precondition_ids",
        "successful_trace_candidate_count", "successful_trace_authority", "revision_candidate", "status", "lifecycle_action", "review_digest",
    }
    if set(review) != required:
        raise ProceduralRevisionReviewError("procedural review has unsupported or missing fields")
    payload = {key: review[key] for key in review if key != "review_digest"}
    if _digest(payload) != review["review_digest"]:
        raise ProceduralRevisionReviewError("procedural review digest mismatch")
    if review["status"] != "operator_review_required" or review["lifecycle_action"] != "none":
        raise ProceduralRevisionReviewError("procedural review is not an operator-only proposal")
    if review["successful_trace_authority"] != "review_required":
        raise ProceduralRevisionReviewError("successful procedure trace cannot become authority")
    if not _is_digest(review["procedure_digest"]) or not _is_digest(review["review_digest"]):
        raise ProceduralRevisionReviewError("procedural review digest is malformed")
    if review["previous_procedure_digest"] is not None and not _is_digest(review["previous_procedure_digest"]):
        raise ProceduralRevisionReviewError("procedural review previous digest is malformed")
    if int(review["outcome_count"]) != int(review["success_count"]) + int(review["failure_count"]):
        raise ProceduralRevisionReviewError("procedural review outcome counters are inconsistent")
    expected_candidate = int(review["failure_count"]) >= MIN_FAILURE_PATTERN and bool(review["previous_procedure_digest"])
    if review["revision_candidate"] is not expected_candidate:
        raise ProceduralRevisionReviewError("procedural review revision candidacy is inconsistent")


def _invalid_sequence(value: object) -> bool:
    return not isinstance(value, Sequence) or isinstance(value, (str, bytes))


def _bounded_text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 64:
        raise ProceduralRevisionReviewError(f"{name} must be a bounded non-empty string")
    return text


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _is_digest(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


__all__ = [
    "MAX_RECEIPTS",
    "MIN_FAILURE_PATTERN",
    "PROCEDURAL_REVISION_REVIEW_SCHEMA_VERSION",
    "ProceduralRevisionReviewError",
    "build_procedural_revision_consolidation_candidate",
    "build_procedural_revision_review",
]
