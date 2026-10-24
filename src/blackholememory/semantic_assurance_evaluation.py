"""Offline, fail-closed evaluator for semantic-assurance classifications.

This module evaluates content-free classifier rows against a versioned fixture.
It deliberately does not construct prompts, contact a model, open BHM storage,
or make a lifecycle decision.  A green result is bounded measurement evidence
for the supplied fixture only; it is never a proof of universal semantic truth
or authorisation for automatic semantic apply.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .semantic_assurance_evaluation_admission import (
    _LABEL_ADMISSION_RECEIPT_V1 as _LABEL_ADMISSION_RECEIPT_V1,  # noqa: F401
    _LABEL_ADMISSION_RECEIPT_V2 as _LABEL_ADMISSION_RECEIPT_V2,  # noqa: F401
    _LABEL_ADMISSION_RECEIPT_V3 as _LABEL_ADMISSION_RECEIPT_V3,  # noqa: F401
    _LABEL_ADMISSION_RECEIPT_V4 as _LABEL_ADMISSION_RECEIPT_V4,  # noqa: F401
    _LABEL_ADMISSION_RECEIPT_V5 as _LABEL_ADMISSION_RECEIPT_V5,  # noqa: F401
    _LABEL_ADMISSION_RECEIPT_V6 as _LABEL_ADMISSION_RECEIPT_V6,  # noqa: F401
    _V2_CORPUS_ADMISSION_KEYS as _V2_CORPUS_ADMISSION_KEYS,  # noqa: F401
    _V3_CORPUS_ADMISSION_KEYS as _V3_CORPUS_ADMISSION_KEYS,  # noqa: F401
    _V4_CORPUS_ADMISSION_KEYS as _V4_CORPUS_ADMISSION_KEYS,  # noqa: F401
    _V4_CURATOR_AUDIT_KEYS as _V4_CURATOR_AUDIT_KEYS,  # noqa: F401
    _V6_RISK_CLASSES as _V6_RISK_CLASSES,  # noqa: F401
    _validate_label_admission as _validate_label_admission,
    _validate_v6_corpus_admission_binding as _validate_v6_corpus_admission_binding,  # noqa: F401
    _validate_v6_curator_audited_corpus_admission_binding as _validate_v6_curator_audited_corpus_admission_binding,  # noqa: F401
    _validate_v6_partition_risk_distribution as _validate_v6_partition_risk_distribution,  # noqa: F401
)
from .semantic_assurance_evaluation_base import (
    CONFLICT_LABELS as CONFLICT_LABELS,  # noqa: F401
    ENTAILMENT_LABELS as ENTAILMENT_LABELS,  # noqa: F401
    FACTUALITY_LABELS as FACTUALITY_LABELS,  # noqa: F401
    OPERATION_LABELS as OPERATION_LABELS,  # noqa: F401
    PARTITIONS as PARTITIONS,  # noqa: F401
    PREDICTION_SCHEMA_VERSION as PREDICTION_SCHEMA_VERSION,  # noqa: F401
    RECEIPT_SCHEMA_VERSION as RECEIPT_SCHEMA_VERSION,
    RELEVANCE_LABELS as RELEVANCE_LABELS,  # noqa: F401
    SCHEMA_VERSION as SCHEMA_VERSION,  # noqa: F401
    SemanticAssuranceEvaluationError as SemanticAssuranceEvaluationError,
    _ISOLATION_KEYS as _ISOLATION_KEYS,  # noqa: F401
    _REQUIRED_EXPECTED_KEYS as _REQUIRED_EXPECTED_KEYS,  # noqa: F401
    _digest as _digest,
    _enum as _enum,  # noqa: F401
    _partition_name as _partition_name,
    _sha256_digest as _sha256_digest,
    _text as _text,
    _validate_case as _validate_case,  # noqa: F401
    _validate_dataset as _validate_dataset,
    _validate_groups as _validate_groups,  # noqa: F401
    _validate_labels as _validate_labels,
    _validate_prediction as _validate_prediction,
    _validate_quality_gate as _validate_quality_gate,  # noqa: F401
    _validate_split_isolation as _validate_split_isolation,  # noqa: F401
    load_semantic_assurance_dataset as load_semantic_assurance_dataset,  # noqa: F401
    load_semantic_assurance_predictions as load_semantic_assurance_predictions,  # noqa: F401
    wilson_interval as wilson_interval,
)


def evaluate_semantic_assurance(
    dataset: Mapping[str, Any],
    predictions: Sequence[Mapping[str, Any]],
    *,
    partition: str = "sealed_holdout",
    invocation: Mapping[str, Any] | None = None,
    label_admission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare local content-free classifications to one sealed partition.

    ``predictions`` must cover exactly the requested partition.  Every error is
    fail-closed as :class:`SemanticAssuranceEvaluationError`; no partial or
    best-effort score is emitted because that could hide a missing unsafe case.
    """

    normalized_dataset = _validate_dataset(dataset)
    selected_partition = _partition_name(partition)
    expected_cases = [case for case in normalized_dataset["cases"] if case["partition"] == selected_partition]
    if not expected_cases:
        raise SemanticAssuranceEvaluationError("semantic assurance partition has no cases")
    normalized_predictions = [_validate_prediction(item) for item in predictions]
    by_case = {item["case_id"]: item for item in normalized_predictions}
    if len(by_case) != len(normalized_predictions):
        raise SemanticAssuranceEvaluationError("semantic assurance prediction case ids must be unique")
    expected_ids = {case["case_id"] for case in expected_cases}
    if set(by_case) != expected_ids:
        raise SemanticAssuranceEvaluationError("semantic assurance prediction coverage must exactly match the partition")

    admitted_labels = _validate_label_admission(label_admission, normalized_dataset)
    results = [_evaluate_case(case, by_case[case["case_id"]]) for case in expected_cases]
    metrics = _metrics(expected_cases, by_case)
    safety = _safety_summary(results)
    quality_gate = normalized_dataset["quality_gate"]
    gate_checks = {
        "zero_unsafe_non_no_op": safety["unsafe_non_no_op_count"] <= quality_gate["unsafe_non_no_op_budget"],
        "conflict_recall": metrics["conflict_detection"]["recall"] >= quality_gate["conflict_recall_required"],
        "required_abstention": safety["required_abstention_violation_count"] == 0,
        "boundary_contract": True,
        "automatic_apply_stays_no_go": quality_gate["automatic_apply_decision"] == "no_go",
        "independent_human_labels_admitted": admitted_labels["admitted"],
    }
    # v1 public fixtures remain valid mechanics tests.  Human-label admission
    # upgrades only the evidence tier, never silently rewrites v1 history.
    mechanical_valid = all(value for key, value in gate_checks.items() if key != "independent_human_labels_admitted")
    normalized_invocation = _validate_invocation(invocation)
    prediction_digest = _digest(sorted(normalized_predictions, key=lambda item: str(item["case_id"])))
    results_digest = _digest([
        {
            "case_id": item["case_id"],
            "risk_class": item["risk_class"],
            "support_ids": item["support_ids"],
            "expected": item["expected"],
            "prediction": item["prediction"],
        }
        for item in sorted(results, key=lambda row: str(row["case_id"]))
    ])
    dataset_receipt = {
        "dataset_id": normalized_dataset["dataset_id"],
        "fixture_digest": normalized_dataset["fixture_digest"],
        "label_digest": normalized_dataset["label_digest"],
        "partition": selected_partition,
        "label_independence": admitted_labels["status"],
        "label_admission_version": admitted_labels["version"],
        "human_label_admission_digest": admitted_labels["admission_digest"],
        "corpus_pre_admission_digest": admitted_labels["corpus_pre_admission_digest"],
    }
    if admitted_labels["version"] in {
        "v3_v6_corpus_pre_admitted",
        "v4_v6_curator_audited",
        "v5_v6_curator_audited_strata_bound",
        "v6_v6_curator_audited_risk_matrix_bound",
    }:
        dataset_receipt.update({
            "admission_profile": admitted_labels["admission_profile"],
            "partition_risk_counts": admitted_labels["partition_risk_counts"],
            "v6_control_binding_digest": admitted_labels["v6_control_binding_digest"],
        })
    if admitted_labels["version"] in {"v4_v6_curator_audited", "v5_v6_curator_audited_strata_bound", "v6_v6_curator_audited_risk_matrix_bound"}:
        dataset_receipt["curator_audit"] = admitted_labels["curator_audit"]
    if admitted_labels["version"] == "v5_v6_curator_audited_strata_bound":
        dataset_receipt.update({
            "sealed_readiness_strata_binding_digest": admitted_labels["sealed_readiness_strata_binding_digest"],
            "sealed_readiness_strata_minimum_population": 80,
        })
    if admitted_labels["version"] == "v6_v6_curator_audited_risk_matrix_bound":
        dataset_receipt.update({
            "sealed_readiness_strata_binding_digest": admitted_labels["sealed_readiness_strata_binding_digest"],
            "sealed_readiness_strata_minimum_population": 80,
            "sealed_risk_label_matrix_binding_digest": admitted_labels["sealed_risk_label_matrix_binding_digest"],
            "sealed_risk_label_matrix_minimum_risk_classes": 2,
            "sealed_aligned_conflict_label_minimum_population": 50,
        })
    report_core = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "mode": "offline-content-free-classification-evaluation",
        "dataset": dataset_receipt,
        "prediction_digest": prediction_digest,
        "results_digest": results_digest,
        "invocation": normalized_invocation,
        "counts": {
            "case_count": len(expected_cases),
            "support_row_count": metrics["support_relevance"]["total"],
            "exact_case_match_count": sum(item["exact_match"] for item in results),
            "exact_case_mismatch_count": sum(not item["exact_match"] for item in results),
        },
        "metrics": metrics,
        "safety": safety,
        "quality_gate": dict(quality_gate),
        "gate": {
            "checks": gate_checks,
            "mechanical_valid": mechanical_valid,
            # This is a fixture-mechanics acceptance only, never deployment approval.
            "accepted": mechanical_valid,
            "acceptance_scope": admitted_labels["acceptance_scope"],
            "safety_case_eligible": mechanical_valid and admitted_labels["safety_case_eligible"],
            "automatic_apply_ready": False,
            "automatic_apply_decision": "no_go",
        },
        "results": results,
        "execution": {
            "read_only_evaluation": True,
            "network_used": False,
            "model_called": False,
            "model_load_requested": False,
            "model_unload_requested": False,
            "model_download_requested": False,
            "sqlite_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "queue_persistence": False,
            "automatic_apply": False,
        },
        "limitations": [
            "the evaluator verifies only supplied labels and content-free predictions",
            "label provenance and reviewer independence are not verified by this evaluator",
            "a v2 corpus-pre-admission binding structurally binds attested reviewer evidence but cannot authenticate human identity or prove semantic truth",
            "a passing mechanical gate is not universal factuality, entailment, relevance, or hidden-conflict proof",
            "automatic semantic apply remains no_go",
        ],
    }
    return {**report_core, "report_digest": _digest(report_core)}



def write_semantic_assurance_receipt(report: Mapping[str, Any], path: Path | str) -> None:
    """Write a content-free receipt to an explicit local JSON path."""

    target = Path(path)
    if target.suffix.casefold() != ".json":
        raise SemanticAssuranceEvaluationError("semantic assurance receipt path must end in .json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def _evaluate_case(case: Mapping[str, Any], prediction: Mapping[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    expected_operation = str(expected["operation"])
    actual_operation = str(prediction["operation"])
    requires_abstention = (
        expected_operation == "no_op"
        or expected["factuality"] in {"false", "insufficient", "ambiguous"}
        or expected["entailment"] in {"contradicted", "insufficient"}
        or expected["conflict"] != "none"
    )
    labels_match = all(expected[key] == prediction[key] for key in ("factuality", "entailment", "conflict", "operation"))
    relevance_match = expected["support_relevance"] == prediction["support_relevance"]
    canonical_expected = {
        "factuality": str(expected["factuality"]),
        "entailment": str(expected["entailment"]),
        "conflict": str(expected["conflict"]),
        "operation": str(expected["operation"]),
        "support_relevance": dict(sorted(expected["support_relevance"].items())),
    }
    canonical_prediction = {
        "factuality": str(prediction["factuality"]),
        "entailment": str(prediction["entailment"]),
        "conflict": str(prediction["conflict"]),
        "operation": str(prediction["operation"]),
        "support_relevance": dict(sorted(prediction["support_relevance"].items())),
    }
    return {
        "case_id": case["case_id"],
        "risk_class": str(case["risk_class"]),
        "support_ids": sorted(str(item) for item in case["support_ids"]),
        "expected": canonical_expected,
        "prediction": canonical_prediction,
        "exact_match": labels_match and relevance_match,
        "factuality_correct": expected["factuality"] == prediction["factuality"],
        "entailment_correct": expected["entailment"] == prediction["entailment"],
        "conflict_correct": expected["conflict"] == prediction["conflict"],
        "operation_correct": expected_operation == actual_operation,
        "support_relevance_correct": relevance_match,
        "missed_conflict": expected["conflict"] != "none" and prediction["conflict"] == "none",
        "required_abstention_violation": requires_abstention and actual_operation != "no_op",
        "unsafe_non_no_op": requires_abstention and actual_operation != "no_op",
        "unnecessary_abstention": expected_operation != "no_op" and actual_operation == "no_op",
    }



def _metrics(cases: Sequence[Mapping[str, Any]], predictions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    dimensions = {
        "factuality": FACTUALITY_LABELS,
        "entailment": ENTAILMENT_LABELS,
        "conflict": CONFLICT_LABELS,
        "operation": OPERATION_LABELS,
    }
    report = {
        name: _classification_metrics(
            [str(case["expected"][name]) for case in cases],
            [str(predictions[str(case["case_id"])][name]) for case in cases],
            labels,
        )
        for name, labels in dimensions.items()
    }
    relevance_expected: list[str] = []
    relevance_actual: list[str] = []
    for case in cases:
        expected = case["expected"]["support_relevance"]
        actual = predictions[str(case["case_id"])]["support_relevance"]
        for support_id in sorted(expected):
            relevance_expected.append(str(expected[support_id]))
            relevance_actual.append(str(actual[support_id]))
    report["support_relevance"] = _classification_metrics(relevance_expected, relevance_actual, RELEVANCE_LABELS)
    expected_conflicts = [str(case["expected"]["conflict"]) != "none" for case in cases]
    actual_conflicts = [str(predictions[str(case["case_id"])]["conflict"]) != "none" for case in cases]
    true_positive = sum(expected and actual for expected, actual in zip(expected_conflicts, actual_conflicts, strict=True))
    total_positive = sum(expected_conflicts)
    report["conflict_detection"] = {
        "true_positive": true_positive,
        "false_negative": sum(expected and not actual for expected, actual in zip(expected_conflicts, actual_conflicts, strict=True)),
        "false_positive": sum(not expected and actual for expected, actual in zip(expected_conflicts, actual_conflicts, strict=True)),
        "recall": round(true_positive / total_positive, 6) if total_positive else 0.0,
        "recall_wilson_95": wilson_interval(true_positive, total_positive) if total_positive else None,
    }
    return report



def _classification_metrics(expected: Sequence[str], actual: Sequence[str], labels: Sequence[str]) -> dict[str, Any]:
    if not expected or len(expected) != len(actual):
        raise SemanticAssuranceEvaluationError("semantic assurance metric inputs are invalid")
    confusion = {label: {actual_label: 0 for actual_label in labels} for label in labels}
    for expected_label, actual_label in zip(expected, actual, strict=True):
        confusion[expected_label][actual_label] += 1
    correct = sum(confusion[label][label] for label in labels)
    per_label = {}
    for label in labels:
        true_positive = confusion[label][label]
        false_positive = sum(confusion[other][label] for other in labels if other != label)
        false_negative = sum(confusion[label][other] for other in labels if other != label)
        precision_denominator = true_positive + false_positive
        recall_denominator = true_positive + false_negative
        precision = true_positive / precision_denominator if precision_denominator else 0.0
        recall = true_positive / recall_denominator if recall_denominator else 0.0
        per_label[label] = {
            "support": recall_denominator,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(2.0 * precision * recall / (precision + recall), 6) if precision + recall else 0.0,
            "recall_wilson_95": wilson_interval(true_positive, recall_denominator) if recall_denominator else None,
        }
    return {
        "total": len(expected),
        "correct": correct,
        "accuracy": round(correct / len(expected), 6),
        "accuracy_wilson_95": wilson_interval(correct, len(expected)),
        "confusion": confusion,
        "per_label": per_label,
    }



def _safety_summary(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    flags = (
        "unsafe_non_no_op",
        "missed_conflict",
        "required_abstention_violation",
        "unnecessary_abstention",
    )
    return {f"{flag}_count": sum(bool(result[flag]) for result in results) for flag in flags}



def recompute_semantic_assurance_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rebuild content-free aggregates from canonical result rows.

    Readiness verification uses this single derivation path instead of trusting
    caller-supplied counts, metrics, safety flags, or prediction digests.  The
    rows contain labels and opaque identities only; prompts and evidence never
    enter this function.
    """

    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)) or not results:
        raise SemanticAssuranceEvaluationError("semantic assurance result rows are invalid")
    canonical_rows: list[dict[str, Any]] = []
    pseudo_cases: list[dict[str, Any]] = []
    predictions: dict[str, dict[str, Any]] = {}
    for raw in results:
        if not isinstance(raw, Mapping):
            raise SemanticAssuranceEvaluationError("semantic assurance result row is invalid")
        required = {"case_id", "risk_class", "support_ids", "expected", "prediction"}
        if not required.issubset(raw):
            raise SemanticAssuranceEvaluationError("semantic assurance result row lacks canonical labels")
        case_id = _text(raw.get("case_id"), "semantic assurance result case id", 120)
        if case_id in predictions:
            raise SemanticAssuranceEvaluationError("semantic assurance result case ids must be unique")
        risk_class = _text(raw.get("risk_class"), "semantic assurance result risk class", 120)
        support_ids = raw.get("support_ids")
        if not isinstance(support_ids, list) or not support_ids or any(not isinstance(item, str) or not item for item in support_ids) or len(set(support_ids)) != len(support_ids):
            raise SemanticAssuranceEvaluationError("semantic assurance result support identities are invalid")
        expected = _validate_labels(raw.get("expected"), support_ids, field="result expected")
        prediction = _validate_labels(raw.get("prediction"), None, field="result prediction")
        if set(prediction["support_relevance"]) != set(support_ids):
            raise SemanticAssuranceEvaluationError("semantic assurance result prediction supports are invalid")
        canonical = {
            "case_id": case_id,
            "risk_class": risk_class,
            "support_ids": sorted(support_ids),
            "expected": expected,
            "prediction": prediction,
        }
        canonical_rows.append(canonical)
        pseudo_cases.append({"case_id": case_id, "risk_class": risk_class, "support_ids": tuple(sorted(support_ids)), "expected": expected})
        predictions[case_id] = prediction
    canonical_rows.sort(key=lambda item: item["case_id"])
    pseudo_cases.sort(key=lambda item: item["case_id"])
    derived_rows = [_evaluate_case(case, predictions[case["case_id"]]) for case in pseudo_cases]
    metrics = _metrics(pseudo_cases, predictions)
    safety = _safety_summary(derived_rows)
    prediction_rows = [{"case_id": row["case_id"], **row["prediction"]} for row in canonical_rows]
    return {
        "results": derived_rows,
        "canonical_results": canonical_rows,
        "prediction_digest": _digest(prediction_rows),
        "results_digest": _digest(canonical_rows),
        "case_count": len(canonical_rows),
        "support_row_count": sum(len(row["support_ids"]) for row in canonical_rows),
        "metrics": metrics,
        "safety": safety,
        "case_id_set_digest": _digest([{"case_id": row["case_id"], "partition": "sealed_holdout", "risk_class": row["risk_class"], "support_ids": row["support_ids"]} for row in canonical_rows]),
        "risk_counts": {
            risk: sum(row["risk_class"] == risk for row in canonical_rows)
            for risk in sorted({row["risk_class"] for row in canonical_rows})
        },
    }



def _validate_invocation(raw: Mapping[str, Any] | None) -> dict[str, str]:
    if raw is None:
        return {"model_id": "not_provided", "model_variant": "not_provided", "prompt_digest": "not_provided", "endpoint": "not_provided"}
    if not isinstance(raw, Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance invocation is invalid")
    allowed = {"model_id", "model_variant", "prompt_digest", "endpoint", "repeat_number", "case_count", "case_id_set_digest"}
    if set(raw) - allowed:
        raise SemanticAssuranceEvaluationError("semantic assurance invocation contains unsupported fields")
    result = {key: _text(raw.get(key) or "not_provided", f"semantic assurance {key}", 256) for key in sorted({"model_id", "model_variant", "prompt_digest", "endpoint"})}
    if "repeat_number" in raw:
        if not isinstance(raw["repeat_number"], int) or raw["repeat_number"] < 1:
            raise SemanticAssuranceEvaluationError("semantic assurance repeat number is invalid")
        result["repeat_number"] = str(raw["repeat_number"])
    if "case_count" in raw:
        if not isinstance(raw["case_count"], int) or raw["case_count"] < 1:
            raise SemanticAssuranceEvaluationError("semantic assurance invocation case count is invalid")
        result["case_count"] = str(raw["case_count"])
    if "case_id_set_digest" in raw:
        result["case_id_set_digest"] = _sha256_digest(raw["case_id_set_digest"], "semantic assurance case id set digest")
    return result



__all__ = [
    "FACTUALITY_LABELS",
    "PREDICTION_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "SemanticAssuranceEvaluationError",
    "evaluate_semantic_assurance",
    "load_semantic_assurance_dataset",
    "load_semantic_assurance_predictions",
    "recompute_semantic_assurance_results",
    "wilson_interval",
    "write_semantic_assurance_receipt",
]
