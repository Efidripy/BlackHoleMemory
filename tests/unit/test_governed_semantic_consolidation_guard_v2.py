"""Read-only regression tests for governed semantic consolidation guard v2.

Run: uv run pytest -q tests/unit/test_governed_semantic_consolidation_guard_v2.py
"""

from blackholememory.governed_semantic_editor import (
    _apply_semantic_policy,
    build_semantic_proposal,
)


def basis(*contents):
    return [{"content": content, "project": "synthetic", "source_id": f"id-{i}"}
            for i, content in enumerate(contents)]


def verdict(*, operation="create", records=(), content="New consolidated insight", conflicts=(),
            confidence=0.85, historical=False):
    return _apply_semantic_policy(
        operation=operation,
        confidence=confidence,
        conflicts=list(conflicts),
        candidate={"title": "Result", "content": content, "memory_type": "fact", "concepts": [], "files": []},
        model_reason="Synthetic test analysis found this memory useful.",
        basis_records=list(records),
        allow_single_historical_rollover=historical,
    )


def test_one_fact_does_not_justify_a_consolidated_create():
    outcome = verdict(records=basis("Config manifest is canonical"))
    assert outcome["operation"] == "no_op"
    assert outcome["receipt"]["decision"] == "proposal_only"
    assert outcome["receipt"]["guard_code"] == "insufficient_distinct_consolidation_basis"
    assert outcome["receipt"]["model_operation"] == "create"
    assert outcome["receipt"]["effective_operation"] == "no_op"


def test_two_distinct_facts_can_form_a_consolidation_candidate():
    outcome = verdict(records=basis("Ledger is authoritative", "Dashboard is derived"))
    assert outcome["operation"] == "create"
    assert outcome["receipt"]["distinct_basis_content_count"] == 2
    assert outcome["receipt"]["model_operation"] == "create"


def test_repeated_candidate_is_refused_even_with_two_basis_facts():
    outcome = verdict(
        records=basis("Ledger is authoritative", "Dashboard is derived"),
        content=" LEDGER  is AUTHORITATIVE ",
    )
    assert outcome["operation"] == "no_op"
    assert outcome["receipt"]["decision"] == "proposal_only"
    assert outcome["receipt"]["guard_code"] == "exact_existing_fact_duplicate"


def test_two_identical_basis_records_are_not_independent():
    outcome = verdict(records=basis("Canonical fact", " canonical  fact "))
    assert outcome["operation"] == "no_op"
    assert outcome["receipt"]["distinct_basis_content_count"] == 1


def test_single_source_revise_path_is_not_changed():
    outcome = verdict(operation="revise", records=basis("Old rule"), content="New evidence supports additive clarification")
    assert outcome["operation"] == "revise"


def test_explicit_historical_rollover_exception_is_separate():
    # In production this flag is generated only after _legacy_trace_memory_id validates typed traces.
    outcome = verdict(records=basis("Old checkpoint trace"), historical=True)
    assert outcome["operation"] == "create"


def test_create_requires_canonical_basis_when_private_policy_is_reused():
    outcome = _apply_semantic_policy(
        operation="create",
        confidence=0.9,
        conflicts=[],
        candidate={"title": "Result", "content": "New fact", "memory_type": "fact", "concepts": [], "files": []},
        model_reason="Synthetic test requires canonical evidence first.",
    )
    assert outcome["operation"] == "no_op"
    assert outcome["receipt"]["decision"] == "canonical_basis_required"


class _Completion:
    def __init__(self, content: str) -> None:
        self.content = content

    def complete(self, **_kwargs):
        return {
            "operation": "create",
            "candidate": {"title": "Successor", "content": self.content, "memory_type": "fact", "concepts": [], "files": [], "evidence_refs": [1, 2]},
            "confidence": 0.9,
            "conflicts": [],
            "reason": "Synthetic test uses bounded same-project evidence.",
        }


def _canonical_record(memory_id: str, content: str, *, trace: bool = False):
    return {
        "id": memory_id,
        "source_id": memory_id,
        "project": "synthetic",
        "memory_type": "workflow" if trace else "fact",
        "content": content,
        "revision_id": f"rev-{memory_id}",
        "event_role": "trace" if trace else "fact",
        "metadata": {
            "revision_id": f"rev-{memory_id}",
            **({"artifact_kind": "checkpoint", "event_role": "trace"} if trace else {}),
        },
    }


def test_mixed_historical_basis_cannot_bypass_duplicate_or_retire_current_fact():
    trace = _canonical_record("trace", "Historical checkpoint", trace=True)
    current = _canonical_record("current", "Current authoritative fact")
    proposal = build_semantic_proposal(
        project="synthetic",
        query="synthetic rollover check",
        retrieved_records=[trace, current],
        completion=_Completion(" Current authoritative fact "),
        retire_historical_basis=True,
    )

    assert proposal["operation"] == "no_op"
    assert proposal["semantic_editor"]["policy"]["guard_code"] == "exact_existing_fact_duplicate"
    assert proposal["semantic_editor"]["legacy_rollover"]["eligible_basis_memory_ids"] == ["trace"]
    assert proposal["semantic_editor"]["legacy_rollover"]["retire_basis_memory_ids"] == []


def test_conflicted_link_is_downgraded_to_noop():
    outcome = verdict(operation="link", records=basis("A", "B"), conflicts=["incompatible relation"])
    assert outcome["operation"] == "no_op"
    assert outcome["receipt"]["decision"] == "conflict_requires_operator_review"


def test_conflict_downgrade_retains_raw_decision_in_receipt():
    outcome = verdict(records=basis("A", "B"), conflicts=["incompatible scopes"])
    assert outcome["operation"] == "no_op"
    assert outcome["receipt"]["model_operation"] == "create"
    assert outcome["receipt"]["effective_operation"] == "no_op"
    assert outcome["receipt"]["model_conflict_count"] == 1


def test_no_op_conflict_telemetry_is_not_silently_erased():
    outcome = verdict(operation="no_op", records=basis("A"), conflicts=["claim"])
    assert outcome["operation"] == "no_op"
    assert outcome["receipt"]["model_conflict_count"] == 1


def test_low_confidence_still_blocks_create():
    outcome = verdict(records=basis("A", "B"), confidence=0.2)
    assert outcome["operation"] == "no_op"
    assert outcome["receipt"]["decision"] == "insufficient_confidence"
