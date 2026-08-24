from __future__ import annotations

import pytest

from blackholememory.domain import Memory
from blackholememory.governed_semantic_editor import GovernedSemanticEditorError
from blackholememory.governed_semantic_editor import build_semantic_proposal
from blackholememory.governed_semantic_editor import select_authoritative_records


def _record(memory_id: str, content: str, *, project: str = "multiserversubgen") -> dict:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": memory_id,
            "project": project,
            "memory_type": "decision",
            "content": content,
            "created_at": "2026-08-24T10:00:00Z",
            "updated_at": "2026-08-24T10:00:00Z",
            "metadata": {"raw_title": memory_id},
        }
    ).to_record()


class _Completion:
    def __init__(self, result: dict) -> None:
        self.result = result

    def complete(self, *, project: str, query: str, records: list[dict]) -> dict:
        assert project == "multiserversubgen"
        assert query
        assert records
        return self.result


def _candidate(*, operation: str = "create", confidence: float = 0.91, conflicts: list[str] | None = None) -> dict:
    return {
        "operation": operation,
        "basis_memory_ids": ["mem_bhm_a", "mem_bhm_b"],
        "candidate": {
            "title": "Installer safety contract",
            "content": "Normal uninstall stays project-scoped and requires an install-state log.",
            "memory_type": "decision",
            "concepts": ["uninstall", "safety"],
            "files": [],
        },
        "confidence": confidence,
        "conflicts": conflicts or [],
        "reason": "same-project evidence agrees",
    }


def test_semantic_editor_generates_only_validated_same_project_proposal() -> None:
    source = [
        _record("mem_bhm_a", "Normal uninstall is project-scoped."),
        _record("mem_bhm_b", "A valid install-state log is required."),
    ]
    proposal = build_semantic_proposal(
        project="multiserversubgen",
        query="uninstall safety",
        retrieved_records=source,
        completion=_Completion(_candidate()),
    )

    assert proposal["operation"] == "create"
    assert [item["memory_id"] for item in proposal["basis"]] == ["mem_bhm_a", "mem_bhm_b"]
    assert proposal["analyzer"] == "bhm-local-semantic-editor/v1"
    assert proposal["semantic_editor"]["retrieved_candidate_count"] == 2
    assert proposal["execution"] == {
        "proposal_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "automatic_apply": False,
        "local_model_called": True,
        "semantic_retrieval": True,
    }


def test_semantic_editor_rejects_model_basis_outside_revalidated_sqlite_records() -> None:
    candidate = _candidate()
    candidate["basis_memory_ids"] = ["mem_bhm_a", "mem_bhm_foreign"]
    with pytest.raises(GovernedSemanticEditorError, match="outside SQLite-revalidated"):
        build_semantic_proposal(
            project="multiserversubgen",
            query="uninstall safety",
            retrieved_records=[_record("mem_bhm_a", "A"), _record("mem_bhm_b", "B")],
            completion=_Completion(candidate),
        )


def test_conflicting_or_low_confidence_semantic_result_becomes_no_op() -> None:
    source = [_record("mem_bhm_a", "A"), _record("mem_bhm_b", "B")]
    conflicting = build_semantic_proposal(
        project="multiserversubgen",
        query="safety",
        retrieved_records=source,
        completion=_Completion(_candidate(conflicts=["facts disagree"])),
    )
    low_confidence = build_semantic_proposal(
        project="multiserversubgen",
        query="safety",
        retrieved_records=source,
        completion=_Completion(_candidate(confidence=0.25)),
    )

    assert conflicting["operation"] == "no_op"
    assert conflicting["semantic_editor"]["policy"]["decision"] == "conflict_requires_operator_review"
    assert low_confidence["operation"] == "no_op"
    assert low_confidence["semantic_editor"]["policy"]["decision"] == "insufficient_confidence"


def test_retrieval_hits_are_re_read_from_sqlite_and_cross_project_rows_fail_closed() -> None:
    canonical = [_record("mem_bhm_a", "A"), _record("mem_bhm_b", "B"), _record("mem_bhm_other", "C", project="other")]
    selected = select_authoritative_records(
        project="multiserversubgen",
        candidate_ids=["mem_bhm_b", "mem_bhm_a"],
        records=canonical,
    )
    assert [item["source_id"] for item in selected] == ["mem_bhm_b", "mem_bhm_a"]
    with pytest.raises(GovernedSemanticEditorError, match="missing or cross-project"):
        select_authoritative_records(
            project="multiserversubgen",
            candidate_ids=["mem_bhm_other"],
            records=canonical,
        )
