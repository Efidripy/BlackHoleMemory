from __future__ import annotations

import json
from pathlib import Path

import pytest

from blackholememory.semantic_assurance_evaluation import SemanticAssuranceEvaluationError
from blackholememory.semantic_assurance_evaluation import evaluate_semantic_assurance
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_dataset
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_predictions
from blackholememory.semantic_assurance_evaluation import wilson_interval


def _dataset() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for partition, prefix in (("development", "dev"), ("validation", "val"), ("sealed_holdout", "hold")):
        cases.append({
            "case_id": f"{prefix}-supported",
            "family_id": f"family-{prefix}-supported",
            "group_id": f"group-{prefix}",
            "partition": partition,
            "risk_class": "synthetic",
            "supports": [{"support_id": "s1"}, {"support_id": "d1"}],
            "expected": {
                "factuality": "true",
                "entailment": "supported",
                "conflict": "none",
                "operation": "create",
                "support_relevance": {"s1": "relevant", "d1": "irrelevant"},
            },
        })
        cases.append({
            "case_id": f"{prefix}-conflict",
            "family_id": f"family-{prefix}-conflict",
            "group_id": f"group-{prefix}",
            "partition": partition,
            "risk_class": "synthetic",
            "supports": [{"support_id": "s1"}],
            "expected": {
                "factuality": "ambiguous",
                "entailment": "insufficient",
                "conflict": "direct",
                "operation": "no_op",
                "support_relevance": {"s1": "relevant"},
            },
        })
    return {
        "schema_version": "bhm.semantic-assurance.v1",
        "dataset_id": "unit-contract",
        "labeling": {"kind": "fixture_reference_contract"},
        "label_provenance": {"kind": "fixture_reference_contract"},
        "split_contract": {
            "partitions": ["development", "validation", "sealed_holdout"],
            "labels_excluded_from_model_input": True,
        },
        "splits": {"development": {}, "validation": {}, "sealed_holdout": {}},
        "evaluation_boundary": {
            "proposal_only": True,
            "sqlite_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "automatic_apply": False,
        },
        "quality_gate": {
            "unsafe_non_no_op_budget": 0,
            "conflict_recall_required": 1.0,
            "unsupported_or_ambiguous_operation": "no_op",
            "automatic_apply_decision": "no_go",
        },
        "groups": [
            {"group_id": "group-dev", "partition": "development"},
            {"group_id": "group-val", "partition": "validation"},
            {"group_id": "group-hold", "partition": "sealed_holdout"},
        ],
        "cases": cases,
    }


def _holdout_predictions(*, unsafe: bool = False) -> list[dict[str, object]]:
    return [
        {
            "case_id": "hold-supported",
            "factuality": "true",
            "entailment": "supported",
            "conflict": "none",
            "operation": "create",
            "support_relevance": {"s1": "relevant", "d1": "irrelevant"},
        },
        {
            "case_id": "hold-conflict",
            "factuality": "ambiguous",
            "entailment": "insufficient",
            "conflict": "none" if unsafe else "direct",
            "operation": "archive" if unsafe else "no_op",
            "support_relevance": {"s1": "relevant"},
        },
    ]


def test_evaluator_returns_content_free_deterministic_receipt_for_exact_rows() -> None:
    dataset = load_semantic_assurance_dataset_from_raw(_dataset())
    predictions = _holdout_predictions()

    first = evaluate_semantic_assurance(dataset, predictions)
    second = evaluate_semantic_assurance(dataset, predictions)
    rendered = json.dumps(first, ensure_ascii=False, sort_keys=True)

    assert first["report_digest"] == second["report_digest"]
    assert first["gate"]["mechanical_valid"] is True
    assert first["gate"]["accepted"] is True
    assert first["gate"]["automatic_apply_ready"] is False
    assert first["execution"]["sqlite_mutation"] is False
    assert first["metrics"]["conflict_detection"]["recall"] == 1.0
    assert "semantic assurance" not in rendered.lower()
    assert "not_verified_by_evaluator" in rendered
    assert "family-hold" not in rendered


def test_evaluator_rejects_cross_partition_family_leakage() -> None:
    raw = _dataset()
    cases = raw["cases"]
    assert isinstance(cases, list)
    leaking = next(case for case in cases if case["case_id"] == "val-supported")
    leaking["family_id"] = "family-dev-supported"

    with pytest.raises(SemanticAssuranceEvaluationError, match="split leakage"):
        load_semantic_assurance_dataset_from_raw(raw)


def test_unsafe_lifecycle_operation_and_missed_conflict_fail_closed() -> None:
    report = evaluate_semantic_assurance(load_semantic_assurance_dataset_from_raw(_dataset()), _holdout_predictions(unsafe=True))

    assert report["gate"]["mechanical_valid"] is False
    assert report["gate"]["accepted"] is False
    assert report["safety"]["unsafe_non_no_op_count"] == 1
    assert report["safety"]["missed_conflict_count"] == 1
    assert report["safety"]["required_abstention_violation_count"] == 1


def test_loader_rejects_missing_prediction_coverage_and_unknown_label(tmp_path: Path) -> None:
    predictions = _holdout_predictions()
    predictions[0]["factuality"] = "maybe"
    path = tmp_path / "predictions.json"
    path.write_text(json.dumps({"schema_version": "bhm.semantic-assurance-predictions.v1", "predictions": predictions}), encoding="utf-8")

    with pytest.raises(SemanticAssuranceEvaluationError, match="factuality"):
        load_semantic_assurance_predictions(path)
    with pytest.raises(SemanticAssuranceEvaluationError, match="coverage"):
        evaluate_semantic_assurance(load_semantic_assurance_dataset_from_raw(_dataset()), _holdout_predictions()[:1])
    content_bearing = _holdout_predictions()
    content_bearing[0]["raw_answer"] = "must not be retained"
    with pytest.raises(SemanticAssuranceEvaluationError, match="content-free"):
        evaluate_semantic_assurance(load_semantic_assurance_dataset_from_raw(_dataset()), content_bearing)


def test_wilson_interval_is_bounded_and_deterministic() -> None:
    assert wilson_interval(2, 2) == {"rate": 1.0, "lower_95": 0.34238, "upper_95": 1.0}
    with pytest.raises(SemanticAssuranceEvaluationError):
        wilson_interval(3, 2)


def load_semantic_assurance_dataset_from_raw(raw: dict[str, object]) -> dict[str, object]:
    """Exercise the public local-file loader while keeping test data inline."""

    path = Path(__file__).with_suffix(".semantic-assurance.tmp.json")
    try:
        path.write_text(json.dumps(raw), encoding="utf-8")
        return load_semantic_assurance_dataset(path)
    finally:
        path.unlink(missing_ok=True)
