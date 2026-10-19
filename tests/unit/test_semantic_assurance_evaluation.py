from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import blackholememory.semantic_assurance_evaluation as evaluation
import blackholememory.semantic_assurance_label_admission as label_admission

from blackholememory.semantic_assurance_evaluation import SemanticAssuranceEvaluationError
from blackholememory.semantic_assurance_evaluation import evaluate_semantic_assurance
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_dataset
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_predictions
from blackholememory.semantic_assurance_evaluation import recompute_semantic_assurance_results
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


def test_v3_preserves_and_recomputes_the_v6_corpus_distribution_before_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V6 evidence must remain bound at the evaluator boundary, not just upstream."""

    dataset = load_semantic_assurance_dataset_from_raw(_v6_dataset())
    pre_admission_digest = "a" * 64
    receipt = _v3_label_admission(dataset, pre_admission_digest)
    monkeypatch.setattr(label_admission, "verify_semantic_assurance_label_admission", lambda value: value)

    report = evaluate_semantic_assurance(
        dataset,
        _v6_holdout_predictions(dataset),
        label_admission=receipt,
    )

    assert report["gate"]["safety_case_eligible"] is True
    assert report["dataset"]["label_admission_version"] == "v3_v6_corpus_pre_admitted"
    assert report["dataset"]["admission_profile"] == "v6_independent_human_curation"
    assert report["dataset"]["partition_risk_counts"] == receipt["corpus_admission"]["partition_risk_counts"]
    assert report["dataset"]["v6_control_binding_digest"] == "7" * 64

    tampered = _v3_label_admission(dataset, pre_admission_digest)
    distribution = tampered["corpus_admission"]["partition_risk_counts"]
    assert isinstance(distribution, dict)
    development = distribution["development"]
    assert isinstance(development, dict)
    development["factuality"] += 1
    with pytest.raises(SemanticAssuranceEvaluationError, match="V6 partition risk distribution"):
        _evaluate_v6_before_scoring(monkeypatch, dataset, tampered)


def test_v4_requires_exact_curator_audit_binding_before_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical-source-audited evidence is a distinct forward-only tier."""

    dataset = load_semantic_assurance_dataset_from_raw(_v6_dataset())
    receipt = _v4_label_admission(dataset, "a" * 64)
    monkeypatch.setattr(label_admission, "verify_semantic_assurance_label_admission", lambda value: value)

    report = evaluate_semantic_assurance(
        dataset,
        _v6_holdout_predictions(dataset),
        label_admission=receipt,
    )

    assert report["dataset"]["label_admission_version"] == "v4_v6_curator_audited"
    assert report["dataset"]["admission_profile"] == "v6_curator_audited_independent_human_curation"
    assert report["dataset"]["curator_audit"]["audit_digest"] == "1" * 64
    assert report["gate"]["automatic_apply_ready"] is False
    assert report["gate"]["automatic_apply_decision"] == "no_go"

    tampered = _v4_label_admission(dataset, "a" * 64)
    audit = tampered["corpus_admission"]["curator_audit"]
    assert isinstance(audit, dict)
    audit["canonical_fragment_binding_digest"] = "not-a-digest"
    with pytest.raises(SemanticAssuranceEvaluationError, match="curator audit canonical_fragment_binding_digest"):
        _evaluate_v6_before_scoring(monkeypatch, dataset, tampered)


def test_v3_cannot_be_upgraded_by_optional_curator_audit_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = load_semantic_assurance_dataset_from_raw(_v6_dataset())
    receipt = _v3_label_admission(dataset, "a" * 64)
    receipt["corpus_admission"]["curator_audit"] = {
        "receipt_schema_version": "bhm.semantic-assurance-curator-audit-receipt.v1",
        "audit_digest": "1" * 64,
        "atomic_fact_binding_digest": "2" * 64,
        "control_binding_digest": "3" * 64,
        "canonical_fragment_binding_digest": "4" * 64,
    }
    monkeypatch.setattr(label_admission, "verify_semantic_assurance_label_admission", lambda value: value)
    with pytest.raises(SemanticAssuranceEvaluationError, match="corpus pre-admission binding"):
        _evaluate_v6_before_scoring(monkeypatch, dataset, receipt)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("profile", "V6 corpus admission tier"),
        ("control", "V6 control binding"),
        ("missing_control", "corpus pre-admission binding"),
    ],
)
def test_v3_rejects_tampered_profile_or_control_before_scoring(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    dataset = load_semantic_assurance_dataset_from_raw(_v6_dataset())
    receipt = _v3_label_admission(dataset, "a" * 64)
    corpus_admission = receipt["corpus_admission"]
    assert isinstance(corpus_admission, dict)
    if mutation == "profile":
        corpus_admission["admission_profile"] = "v2_corpus_curation"
    elif mutation == "control":
        corpus_admission["v6_control_binding_digest"] = "not-a-digest"
    else:
        del corpus_admission["v6_control_binding_digest"]

    with pytest.raises(SemanticAssuranceEvaluationError, match=message):
        _evaluate_v6_before_scoring(monkeypatch, dataset, receipt)


@pytest.mark.parametrize(
    "legacy_schema",
    [
        "bhm.semantic-assurance-human-label-admission-receipt.v1",
        "bhm.semantic-assurance-human-label-admission-receipt.v2",
    ],
)
def test_legacy_label_admission_paths_reject_v3_payloads_before_scoring(
    monkeypatch: pytest.MonkeyPatch,
    legacy_schema: str,
) -> None:
    dataset = load_semantic_assurance_dataset_from_raw(_v6_dataset())
    receipt = _v3_label_admission(dataset, "a" * 64)
    receipt["schema_version"] = legacy_schema

    with pytest.raises(SemanticAssuranceEvaluationError, match="human-label admission corpus|corpus pre-admission binding"):
        _evaluate_v6_before_scoring(monkeypatch, dataset, receipt)


def _evaluate_v6_before_scoring(
    monkeypatch: pytest.MonkeyPatch,
    dataset: dict[str, object],
    receipt: dict[str, object],
) -> None:
    monkeypatch.setattr(label_admission, "verify_semantic_assurance_label_admission", lambda value: value)
    monkeypatch.setattr(
        evaluation,
        "_evaluate_case",
        lambda *_args: (_ for _ in ()).throw(AssertionError("evaluation reached scoring")),
    )
    evaluate_semantic_assurance(dataset, _v6_holdout_predictions(dataset), label_admission=receipt)


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


def test_recomputed_per_label_metrics_cover_every_readiness_stratum() -> None:
    """Readiness consumes exact-label metrics derived from canonical rows only."""

    labels = [
        ("false", "supported", "direct"),
        ("ambiguous", "supported", "temporal"),
        ("insufficient", "insufficient", "scope"),
        ("true", "contradicted", "implicit"),
        ("true", "insufficient", "none"),
    ]
    rows = [
        {
            "case_id": f"case-{index}",
            "risk_class": "unit",
            "support_ids": [f"support-{index}"],
            "expected": {
                "factuality": factuality,
                "entailment": entailment,
                "conflict": conflict,
                "operation": "no_op",
                "support_relevance": {f"support-{index}": "irrelevant"},
            },
            "prediction": {
                "factuality": factuality,
                "entailment": entailment,
                "conflict": conflict,
                "operation": "no_op",
                "support_relevance": {f"support-{index}": "irrelevant"},
            },
        }
        for index, (factuality, entailment, conflict) in enumerate(labels, start=1)
    ]

    metrics = recompute_semantic_assurance_results(rows)["metrics"]
    expected_strata = {
        ("factuality", "false"): 1,
        ("factuality", "ambiguous"): 1,
        ("factuality", "insufficient"): 1,
        ("entailment", "contradicted"): 1,
        ("entailment", "insufficient"): 2,
        ("support_relevance", "irrelevant"): 5,
        ("conflict", "direct"): 1,
        ("conflict", "temporal"): 1,
        ("conflict", "scope"): 1,
        ("conflict", "implicit"): 1,
    }

    for (dimension, label), support in expected_strata.items():
        per_label = metrics[dimension]["per_label"][label]
        assert set(per_label) == {
            "support",
            "true_positive",
            "false_positive",
            "false_negative",
            "precision",
            "recall",
            "f1",
            "recall_wilson_95",
        }
        assert per_label["support"] == support
        assert per_label["true_positive"] == support
        assert per_label["recall"] == 1.0
        assert per_label["recall_wilson_95"] == wilson_interval(support, support)


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


def _v6_dataset() -> dict[str, object]:
    risks = (
        "factuality",
        "entailment",
        "relevance",
        "direct_conflict",
        "temporal_conflict",
        "scope_conflict",
        "implicit_conflict",
        "injection",
        "cross_project",
        "retrieval_pressure",
        "abstention",
    )
    cases: list[dict[str, object]] = []
    groups: list[dict[str, str]] = []
    for partition in ("development", "validation", "sealed_holdout"):
        groups.append({"group_id": f"v6-{partition}", "partition": partition})
        for risk in risks:
            conflict = {
                "direct_conflict": "direct",
                "temporal_conflict": "temporal",
                "scope_conflict": "scope",
                "implicit_conflict": "implicit",
            }.get(risk, "none")
            cases.append({
                "case_id": f"{partition}-{risk}",
                "family_id": f"family-{partition}-{risk}",
                "group_id": f"v6-{partition}",
                "partition": partition,
                "risk_class": risk,
                "supports": [{"support_id": "s1"}],
                "expected": {
                    "factuality": "true",
                    "entailment": "supported",
                    "conflict": conflict,
                    "operation": "no_op",
                    "support_relevance": {"s1": "relevant"},
                },
            })
    return {
        "schema_version": "bhm.semantic-assurance.v1",
        "dataset_id": "v6-evaluator-contract",
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
        "groups": groups,
        "cases": cases,
    }


def _v6_holdout_predictions(dataset: dict[str, object]) -> list[dict[str, object]]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    return [
        {
            "case_id": case["case_id"],
            **case["expected"],
        }
        for case in cases
        if case["partition"] == "sealed_holdout"
    ]


def _v3_label_admission(dataset: dict[str, object], admission_digest: str) -> dict[str, object]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    rows = sorted([
        {
            "case_id": case["case_id"],
            "partition": case["partition"],
            "risk_class": case["risk_class"],
            "support_ids": ["s1"],
        }
        for case in cases
    ], key=lambda row: str(row["case_id"]))
    sealed_rows = [row for row in rows if row["partition"] == "sealed_holdout"]
    risks = sorted({str(row["risk_class"]) for row in rows})
    partitions = ("development", "validation", "sealed_holdout")
    corpus_admission = {
        "receipt_schema_version": "bhm.semantic-assurance-corpus-admission-receipt.v2",
        "admission_digest": admission_digest,
        "case_count": len(rows),
        "partition_counts": {partition: sum(row["partition"] == partition for row in rows) for partition in partitions},
        "sealed_support_count": len(sealed_rows),
        "packet_payload_set_digest": "d" * 64,
        "curator_ledger_digest": "e" * 64,
        "curator_attestation_digest": "f" * 64,
        "dataset_binding_digest": _canonical_digest(rows),
        "sealed_case_binding_digest": _canonical_digest(sealed_rows),
        "sealed_support_binding_digest": _canonical_digest([
            {"case_id": row["case_id"], "support_ids": row["support_ids"]}
            for row in sealed_rows
        ]),
        "risk_counts": {risk: sum(row["risk_class"] == risk for row in rows) for risk in risks},
        "sealed_risk_counts": {risk: sum(row["risk_class"] == risk for row in sealed_rows) for risk in risks},
        "admission_profile": "v6_independent_human_curation",
        "partition_risk_counts": {
            partition: {
                risk: sum(row["partition"] == partition and row["risk_class"] == risk for row in rows)
                for risk in risks
            }
            for partition in partitions
        },
        "v6_control_binding_digest": "7" * 64,
    }
    return {
        "schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v3",
        "corpus": {
            "dataset_id": dataset["dataset_id"],
            "fixture_digest": dataset["fixture_digest"],
            "label_digest": dataset["label_digest"],
            "corpus_pre_admission_digest": admission_digest,
        },
        "corpus_admission": corpus_admission,
        "admission_digest": "h" * 64,
    }


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _v4_label_admission(dataset: dict[str, object], admission_digest: str) -> dict[str, object]:
    receipt = _v3_label_admission(dataset, admission_digest)
    corpus_admission = receipt["corpus_admission"]
    assert isinstance(corpus_admission, dict)
    corpus_admission["receipt_schema_version"] = "bhm.semantic-assurance-corpus-admission-receipt.v3"
    corpus_admission["admission_profile"] = "v6_curator_audited_independent_human_curation"
    corpus_admission["curator_audit"] = {
        "receipt_schema_version": "bhm.semantic-assurance-curator-audit-receipt.v1",
        "audit_digest": "1" * 64,
        "atomic_fact_binding_digest": "2" * 64,
        "control_binding_digest": "3" * 64,
        "canonical_fragment_binding_digest": "4" * 64,
    }
    receipt["schema_version"] = "bhm.semantic-assurance-human-label-admission-receipt.v4"
    return receipt
