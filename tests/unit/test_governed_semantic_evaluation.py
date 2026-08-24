from __future__ import annotations

from pathlib import Path

from blackholememory.governed_semantic_evaluation import evaluate_golden_cases
from blackholememory.governed_semantic_evaluation import load_golden_dataset
from blackholememory.governed_semantic_evaluation import summarize_shadow_proposals


_GOLDEN = Path(__file__).parents[1] / "fixtures" / "governed-semantic-editor-golden.json"


def _proposal(case: dict) -> dict:
    return {
        "operation": case["expected_operation"],
        "conflicts": case["expected_conflicts"],
        "execution": {
            "proposal_only": True,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "automatic_apply": False,
        },
    }


def test_redacted_multisubgen_golden_dataset_has_30_bounded_cases() -> None:
    cases = load_golden_dataset(_GOLDEN)
    report = evaluate_golden_cases(cases, _proposal)

    assert len(cases) == 30
    assert report["passed"] == 30
    assert report["failed"] == 0
    assert report["execution"]["model_started"] is False
    assert report["execution"]["sqlite_mutation"] is False


def test_shadow_metrics_are_content_free_and_honest_before_operator_labels() -> None:
    report = summarize_shadow_proposals(
        [
            {
                "analyzer": "bhm-local-semantic-editor/v1",
                "operation": "create",
                "status": "proposed",
                "semantic_editor": {"policy": {"decision": "proposal_only"}},
            },
            {
                "analyzer": "bhm-local-semantic-editor/v1",
                "operation": "no_op",
                "status": "rejected",
                "semantic_editor": {"policy": {"decision": "insufficient_confidence"}},
            },
            {"analyzer": "bhm-native-deterministic/v1", "operation": "create", "status": "applied"},
        ]
    )

    assert report["proposal_count"] == 2
    assert report["review_queue_count"] == 1
    assert report["operator_acceptance_rate"] == 0.0
    assert report["direct_mem0_writes"] is False
    assert report["automatic_apply"] is False
