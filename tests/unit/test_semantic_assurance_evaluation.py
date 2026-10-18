from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import blackholememory.semantic_assurance_label_admission as label_admission

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


def test_only_v2_corpus_pre_admission_can_make_a_mechanical_report_safety_case_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v1 structural receipt is never a substitute for corpus pre-admission."""

    dataset = load_semantic_assurance_dataset_from_raw(_dataset())
    corpus = {
        "dataset_id": dataset["dataset_id"],
        "fixture_digest": dataset["fixture_digest"],
        "label_digest": dataset["label_digest"],
    }
    pre_admission_digest = "a" * 64
    v1 = {
        "schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v1",
        "corpus": corpus,
        "admission_digest": "b" * 64,
    }
    v2 = {
        "schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v2",
        "corpus": {**corpus, "corpus_pre_admission_digest": pre_admission_digest},
        "corpus_admission": _v2_corpus_admission(dataset, pre_admission_digest),
        "admission_digest": "c" * 64,
    }
    monkeypatch.setattr(label_admission, "verify_semantic_assurance_label_admission", lambda value: value)

    v1_report = evaluate_semantic_assurance(dataset, _holdout_predictions(), label_admission=v1)
    v2_report = evaluate_semantic_assurance(dataset, _holdout_predictions(), label_admission=v2)

    assert v1_report["gate"]["mechanical_valid"] is True
    assert v1_report["gate"]["accepted"] is True
    assert v1_report["gate"]["safety_case_eligible"] is False
    assert v1_report["dataset"]["label_admission_version"] == "v1_fixture_mechanics_only"
    assert v2_report["gate"]["safety_case_eligible"] is True
    assert v2_report["dataset"]["corpus_pre_admission_digest"] == pre_admission_digest


def test_v2_rejects_nonidentical_corpus_pre_admission_binding_before_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = load_semantic_assurance_dataset_from_raw(_dataset())
    receipt = {
        "schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v2",
        "corpus": {
            "dataset_id": dataset["dataset_id"],
            "fixture_digest": dataset["fixture_digest"],
            "label_digest": dataset["label_digest"],
            "corpus_pre_admission_digest": "a" * 64,
        },
        "corpus_admission": _v2_corpus_admission(dataset, "b" * 64),
        "admission_digest": "c" * 64,
    }
    monkeypatch.setattr(label_admission, "verify_semantic_assurance_label_admission", lambda value: value)

    with pytest.raises(SemanticAssuranceEvaluationError, match="corpus pre-admission digest"):
        evaluate_semantic_assurance(dataset, _holdout_predictions(), label_admission=receipt)


def test_v2_rejects_invalid_curator_attestation_binding_before_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = load_semantic_assurance_dataset_from_raw(_dataset())
    pre_admission_digest = "a" * 64
    receipt = {
        "schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v2",
        "corpus": {
            "dataset_id": dataset["dataset_id"],
            "fixture_digest": dataset["fixture_digest"],
            "label_digest": dataset["label_digest"],
            "corpus_pre_admission_digest": pre_admission_digest,
        },
        "corpus_admission": {
            **_v2_corpus_admission(dataset, pre_admission_digest),
            "curator_attestation_digest": "not-a-digest",
        },
        "admission_digest": "c" * 64,
    }
    monkeypatch.setattr(label_admission, "verify_semantic_assurance_label_admission", lambda value: value)

    with pytest.raises(SemanticAssuranceEvaluationError, match="curator attestation"):
        evaluate_semantic_assurance(dataset, _holdout_predictions(), label_admission=receipt)


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


def _v2_corpus_admission(dataset: dict[str, object], admission_digest: str) -> dict[str, object]:
    """Construct the evaluator-facing content-free binding from this fixture.

    These tests monkeypatch the upstream receipt verifier to exercise the
    evaluator's downstream binding checks in isolation.  The fixture therefore
    mirrors its declared dataset exactly; it is not an admissible real V2 corpus
    and must never substitute for corpus-admission coverage tests.
    """

    cases = dataset["cases"]
    assert isinstance(cases, list)
    rows = [
        {
            "case_id": case["case_id"],
            "partition": case["partition"],
            "risk_class": case["risk_class"],
            "support_ids": sorted(case["support_ids"]),
        }
        for case in cases
        if isinstance(case, dict)
    ]
    rows.sort(key=lambda row: str(row["case_id"]))
    sealed_rows = [row for row in rows if row["partition"] == "sealed_holdout"]
    risks = sorted({str(row["risk_class"]) for row in rows})
    risk_counts = {risk: sum(row["risk_class"] == risk for row in rows) for risk in risks}
    sealed_risk_counts = {risk: sum(row["risk_class"] == risk for row in sealed_rows) for risk in risks}

    return {
        "receipt_schema_version": "bhm.semantic-assurance-corpus-admission-receipt.v1",
        "admission_digest": admission_digest,
        "case_count": len(rows),
        "partition_counts": {
            partition: sum(row["partition"] == partition for row in rows)
            for partition in ("development", "sealed_holdout", "validation")
        },
        "sealed_support_count": sum(len(row["support_ids"]) for row in sealed_rows),
        "packet_payload_set_digest": "d" * 64,
        "curator_ledger_digest": "e" * 64,
        "curator_attestation_digest": "f" * 64,
        "dataset_binding_digest": _canonical_digest(rows),
        "sealed_case_binding_digest": _canonical_digest(sealed_rows),
        "sealed_support_binding_digest": _canonical_digest([
            {"case_id": row["case_id"], "support_ids": row["support_ids"]}
            for row in sealed_rows
        ]),
        "risk_counts": risk_counts,
        "sealed_risk_counts": sealed_risk_counts,
    }


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
