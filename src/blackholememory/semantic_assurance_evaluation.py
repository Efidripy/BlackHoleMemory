"""Offline, fail-closed evaluator for semantic-assurance classifications.

This module evaluates content-free classifier rows against a versioned fixture.
It deliberately does not construct prompts, contact a model, open BHM storage,
or make a lifecycle decision.  A green result is bounded measurement evidence
for the supplied fixture only; it is never a proof of universal semantic truth
or authorisation for automatic semantic apply.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "bhm.semantic-assurance.v1"
RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-receipt.v1"
PREDICTION_SCHEMA_VERSION = "bhm.semantic-assurance-predictions.v1"
_LABEL_ADMISSION_RECEIPT_V1 = "bhm.semantic-assurance-human-label-admission-receipt.v1"
_LABEL_ADMISSION_RECEIPT_V2 = "bhm.semantic-assurance-human-label-admission-receipt.v2"
_LABEL_ADMISSION_RECEIPT_V3 = "bhm.semantic-assurance-human-label-admission-receipt.v3"
_LABEL_ADMISSION_RECEIPT_V4 = "bhm.semantic-assurance-human-label-admission-receipt.v4"
_LABEL_ADMISSION_RECEIPT_V5 = "bhm.semantic-assurance-human-label-admission-receipt.v5"
_V2_CORPUS_ADMISSION_KEYS = frozenset({
    "receipt_schema_version",
    "admission_digest",
    "case_count",
    "partition_counts",
    "sealed_support_count",
    "packet_payload_set_digest",
    "curator_ledger_digest",
    "curator_attestation_digest",
    "dataset_binding_digest",
    "sealed_case_binding_digest",
    "sealed_support_binding_digest",
    "risk_counts",
    "sealed_risk_counts",
})
_V6_RISK_CLASSES = frozenset({
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
})
_V3_CORPUS_ADMISSION_KEYS = _V2_CORPUS_ADMISSION_KEYS | frozenset({
    "admission_profile",
    "partition_risk_counts",
    "v6_control_binding_digest",
})
_V4_CURATOR_AUDIT_KEYS = frozenset({
    "receipt_schema_version",
    "audit_digest",
    "atomic_fact_binding_digest",
    "control_binding_digest",
    "canonical_fragment_binding_digest",
})
_V4_CORPUS_ADMISSION_KEYS = _V2_CORPUS_ADMISSION_KEYS | frozenset({
    "admission_profile",
    "partition_risk_counts",
    "v6_control_binding_digest",
    "curator_audit",
})
PARTITIONS = frozenset({"development", "validation", "sealed_holdout"})
FACTUALITY_LABELS = ("true", "false", "insufficient", "ambiguous")
ENTAILMENT_LABELS = ("supported", "contradicted", "insufficient")
CONFLICT_LABELS = ("none", "direct", "temporal", "scope", "implicit")
RELEVANCE_LABELS = ("relevant", "irrelevant")
OPERATION_LABELS = ("no_op", "create", "revise", "supersede", "archive", "link")
_ISOLATION_KEYS = ("family_id", "group_id", "source_group_id", "source_theme", "prompt_template", "prompt_template_id", "paraphrase_cluster_id")
_REQUIRED_EXPECTED_KEYS = frozenset({"factuality", "entailment", "conflict", "support_relevance", "operation"})


class SemanticAssuranceEvaluationError(ValueError):
    """The fixture, predictions, or strict safety contract is invalid."""


def load_semantic_assurance_dataset(path: Path | str) -> dict[str, Any]:
    """Load and validate a local semantic-assurance fixture.

    The normalized return value retains only classification metadata and labels,
    never fixture prompt/evidence text.  This makes it safe to pass onward to
    receipt generation without accidentally serialising source material.
    """

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticAssuranceEvaluationError("semantic assurance dataset is unreadable") from exc
    return _validate_dataset(raw)


def load_semantic_assurance_predictions(path: Path | str) -> list[dict[str, Any]]:
    """Load bounded, content-free local classifier output rows."""

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticAssuranceEvaluationError("semantic assurance predictions are unreadable") from exc
    if isinstance(raw, Mapping):
        schema = raw.get("schema_version")
        if schema not in {None, PREDICTION_SCHEMA_VERSION}:
            raise SemanticAssuranceEvaluationError("semantic assurance prediction schema is unsupported")
        raw = raw.get("predictions")
    if not isinstance(raw, list) or not raw:
        raise SemanticAssuranceEvaluationError("semantic assurance predictions must be a non-empty list")
    normalized = [_validate_prediction(item) for item in raw]
    if len({item["case_id"] for item in normalized}) != len(normalized):
        raise SemanticAssuranceEvaluationError("semantic assurance prediction case ids must be unique")
    return normalized


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
    }:
        dataset_receipt.update({
            "admission_profile": admitted_labels["admission_profile"],
            "partition_risk_counts": admitted_labels["partition_risk_counts"],
            "v6_control_binding_digest": admitted_labels["v6_control_binding_digest"],
        })
    if admitted_labels["version"] == "v4_v6_curator_audited":
        dataset_receipt["curator_audit"] = admitted_labels["curator_audit"]
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


def wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054) -> dict[str, float]:
    """Return a deterministic two-sided Wilson interval for a binomial rate."""

    if not isinstance(successes, int) or not isinstance(total, int) or total < 1 or not 0 <= successes <= total:
        raise SemanticAssuranceEvaluationError("wilson interval inputs are invalid")
    if not isinstance(z, (float, int)) or not math.isfinite(float(z)) or float(z) <= 0:
        raise SemanticAssuranceEvaluationError("wilson interval z score is invalid")
    rate = successes / total
    z_squared = float(z) ** 2
    denominator = 1.0 + z_squared / total
    centre = (rate + z_squared / (2.0 * total)) / denominator
    margin = float(z) * math.sqrt((rate * (1.0 - rate) + z_squared / (4.0 * total)) / total) / denominator
    return {"rate": round(rate, 6), "lower_95": round(max(0.0, centre - margin), 6), "upper_95": round(min(1.0, centre + margin), 6)}


def _validate_dataset(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping) and raw.get("_semantic_assurance_normalized") is True:
        return dict(raw)
    if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
        raise SemanticAssuranceEvaluationError("semantic assurance dataset schema is unsupported")
    dataset_id = _text(raw.get("dataset_id"), "semantic assurance dataset id", 120)
    if not isinstance(raw.get("labeling"), Mapping) or not isinstance(raw.get("label_provenance", raw.get("labeling")), Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance labeling metadata is invalid")
    split_contract = raw.get("split_contract")
    if not isinstance(split_contract, Mapping) or split_contract.get("labels_excluded_from_model_input") is not True:
        raise SemanticAssuranceEvaluationError("semantic assurance split contract must exclude labels from model input")
    declared_partitions = split_contract.get("partitions")
    if not isinstance(declared_partitions, list) or set(declared_partitions) != PARTITIONS:
        raise SemanticAssuranceEvaluationError("semantic assurance partitions are invalid")
    splits = raw.get("splits")
    if not isinstance(splits, Mapping) or set(splits) != PARTITIONS:
        raise SemanticAssuranceEvaluationError("semantic assurance split metadata is invalid")
    boundary = raw.get("evaluation_boundary")
    if not isinstance(boundary, Mapping) or any(boundary.get(key) is not expected for key, expected in {
        "proposal_only": True,
        "sqlite_mutation": False,
        "mem0_mutation": False,
        "qdrant_mutation": False,
        "automatic_apply": False,
    }.items()):
        raise SemanticAssuranceEvaluationError("semantic assurance boundary contract is unsafe")
    quality_gate = _validate_quality_gate(raw.get("quality_gate"))
    groups = _validate_groups(raw.get("groups"))
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SemanticAssuranceEvaluationError("semantic assurance dataset must contain cases")
    normalized_cases = [_validate_case(case, groups) for case in cases]
    if len({case["case_id"] for case in normalized_cases}) != len(normalized_cases):
        raise SemanticAssuranceEvaluationError("semantic assurance case ids must be unique")
    if {case["partition"] for case in normalized_cases} != PARTITIONS:
        raise SemanticAssuranceEvaluationError("semantic assurance dataset must cover every partition")
    _validate_split_isolation(normalized_cases)
    labels_only = [{
        "case_id": case["case_id"],
        "partition": case["partition"],
        "expected": case["expected"],
    } for case in normalized_cases]
    return {
        "_semantic_assurance_normalized": True,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "labeling_kind": _text(raw["labeling"].get("kind"), "semantic assurance labeling kind", 120),
        "label_provenance_kind": _text(raw["label_provenance"].get("kind"), "semantic assurance label provenance kind", 120),
        "cases": normalized_cases,
        "quality_gate": quality_gate,
        "fixture_digest": _digest(raw),
        "label_digest": _digest(labels_only),
    }


def _validate_quality_gate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "unsafe_non_no_op_budget", "conflict_recall_required", "unsupported_or_ambiguous_operation", "automatic_apply_decision"
    }:
        raise SemanticAssuranceEvaluationError("semantic assurance quality gate is invalid")
    budget = raw.get("unsafe_non_no_op_budget")
    if not isinstance(budget, int) or budget != 0:
        raise SemanticAssuranceEvaluationError("semantic assurance quality gate must retain zero unsafe-operation budget")
    try:
        conflict_recall = float(raw.get("conflict_recall_required"))
    except (TypeError, ValueError) as exc:
        raise SemanticAssuranceEvaluationError("semantic assurance conflict recall gate is invalid") from exc
    if conflict_recall != 1.0 or raw.get("unsupported_or_ambiguous_operation") != "no_op" or raw.get("automatic_apply_decision") != "no_go":
        raise SemanticAssuranceEvaluationError("semantic assurance quality gate is unsafe")
    return {
        "unsafe_non_no_op_budget": 0,
        "conflict_recall_required": 1.0,
        "unsupported_or_ambiguous_operation": "no_op",
        "automatic_apply_decision": "no_go",
    }


def _validate_groups(raw: Any) -> dict[str, str]:
    if not isinstance(raw, list) or not raw:
        raise SemanticAssuranceEvaluationError("semantic assurance groups are invalid")
    result: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise SemanticAssuranceEvaluationError("semantic assurance group is invalid")
        group_id = _text(item.get("group_id"), "semantic assurance group id", 120)
        partition = _partition_name(item.get("partition"))
        if group_id in result:
            raise SemanticAssuranceEvaluationError("semantic assurance group ids must be unique")
        result[group_id] = partition
    return result


def _validate_case(raw: Any, groups: Mapping[str, str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance case is invalid")
    case_id = _text(raw.get("case_id"), "semantic assurance case id", 120)
    family_id = _text(raw.get("family_id"), "semantic assurance family id", 160)
    group_id = _text(raw.get("group_id"), "semantic assurance group id", 120)
    partition = _partition_name(raw.get("partition"))
    if groups.get(group_id) != partition:
        raise SemanticAssuranceEvaluationError("semantic assurance case group partition is invalid")
    risk_class = _text(raw.get("risk_class"), "semantic assurance risk class", 120)
    supports = raw.get("supports")
    if not isinstance(supports, list) or not supports:
        raise SemanticAssuranceEvaluationError("semantic assurance supports are invalid")
    support_ids = []
    for support in supports:
        if not isinstance(support, Mapping):
            raise SemanticAssuranceEvaluationError("semantic assurance support is invalid")
        support_ids.append(_text(support.get("support_id"), "semantic assurance support id", 120))
    if len(set(support_ids)) != len(support_ids):
        raise SemanticAssuranceEvaluationError("semantic assurance support ids must be unique per case")
    expected = _validate_labels(raw.get("expected"), support_ids, field="expected")
    isolation = {"family_id": family_id, "group_id": group_id}
    for key in _ISOLATION_KEYS[2:]:
        if key in raw:
            isolation[key] = _text(raw.get(key), f"semantic assurance {key}", 160)
    # Preserve only opaque support identities.  They are required to bind
    # independent relevance labels, but reveal neither prompt nor evidence.
    return {
        "case_id": case_id,
        "partition": partition,
        "support_ids": tuple(sorted(support_ids)),
        "risk_class": risk_class,
        "expected": expected,
        "isolation": isolation,
    }


def _validate_prediction(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance prediction is invalid")
    allowed = {"case_id", *_REQUIRED_EXPECTED_KEYS}
    if set(raw) != allowed:
        raise SemanticAssuranceEvaluationError("semantic assurance prediction must be content-free classification only")
    case_id = _text(raw.get("case_id"), "semantic assurance prediction case id", 120)
    labels = _validate_labels({key: raw[key] for key in _REQUIRED_EXPECTED_KEYS}, None, field="prediction")
    return {"case_id": case_id, **labels}


def _validate_labels(raw: Any, support_ids: Sequence[str] | None, *, field: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _REQUIRED_EXPECTED_KEYS:
        raise SemanticAssuranceEvaluationError(f"semantic assurance {field} labels are incomplete")
    factuality = _enum(raw.get("factuality"), FACTUALITY_LABELS, f"semantic assurance {field} factuality")
    entailment = _enum(raw.get("entailment"), ENTAILMENT_LABELS, f"semantic assurance {field} entailment")
    conflict = _enum(raw.get("conflict"), CONFLICT_LABELS, f"semantic assurance {field} conflict")
    operation = _enum(raw.get("operation"), OPERATION_LABELS, f"semantic assurance {field} operation")
    relevance = raw.get("support_relevance")
    if not isinstance(relevance, Mapping) or not relevance:
        raise SemanticAssuranceEvaluationError(f"semantic assurance {field} relevance is invalid")
    relevance_normalized = {
        _text(key, f"semantic assurance {field} support id", 120): _enum(value, RELEVANCE_LABELS, f"semantic assurance {field} relevance")
        for key, value in relevance.items()
    }
    if len(relevance_normalized) != len(relevance):
        raise SemanticAssuranceEvaluationError(f"semantic assurance {field} relevance ids are invalid")
    if support_ids is not None and set(relevance_normalized) != set(support_ids):
        raise SemanticAssuranceEvaluationError(f"semantic assurance {field} relevance must cover exact supports")
    return {
        "factuality": factuality,
        "entailment": entailment,
        "conflict": conflict,
        "operation": operation,
        "support_relevance": dict(sorted(relevance_normalized.items())),
    }


def _validate_split_isolation(cases: Sequence[Mapping[str, Any]]) -> None:
    observed: dict[tuple[str, str], str] = {}
    for case in cases:
        for key, value in dict(case["isolation"]).items():
            identity = (key, str(value))
            existing = observed.setdefault(identity, str(case["partition"]))
            if existing != case["partition"]:
                raise SemanticAssuranceEvaluationError(f"semantic assurance split leakage for {key}")


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


def _validate_label_admission(raw: Mapping[str, Any] | None, dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Bind an optional content-free dual-review receipt to this exact corpus.

    The import stays local to avoid a module cycle: the admission contract uses
    the label enums from this module, while this evaluator only needs its final
    receipt verifier.  A missing receipt is explicit lower-tier evidence, not
    a permissive substitute.
    """

    if raw is None:
        return {
            "admitted": False,
            "status": "not_verified_by_evaluator",
            "version": "none",
            "admission_digest": "not_provided",
            "corpus_pre_admission_digest": "not_provided",
            "safety_case_eligible": False,
            "acceptance_scope": "proposal_only_fixture_measurement",
        }
    try:
        from .semantic_assurance_label_admission import SemanticAssuranceLabelAdmissionError
        from .semantic_assurance_label_admission import verify_semantic_assurance_label_admission

        receipt = verify_semantic_assurance_label_admission(raw)
    except SemanticAssuranceLabelAdmissionError as exc:
        raise SemanticAssuranceEvaluationError("semantic assurance human-label admission is invalid") from exc
    corpus = receipt["corpus"]
    expected = {
        "dataset_id": dataset["dataset_id"],
        "fixture_digest": dataset["fixture_digest"],
        "label_digest": dataset["label_digest"],
    }
    if not isinstance(corpus, Mapping) or any(corpus.get(key) != value for key, value in expected.items()):
        raise SemanticAssuranceEvaluationError("semantic assurance human-label admission does not bind this exact dataset")
    schema_version = receipt.get("schema_version")
    if schema_version == _LABEL_ADMISSION_RECEIPT_V1:
        if set(corpus) != set(expected):
            raise SemanticAssuranceEvaluationError("semantic assurance v1 human-label admission corpus is invalid")
        return {
            "admitted": True,
            "status": "attested_structurally_bound_not_identity_authenticated",
            "version": "v1_fixture_mechanics_only",
            "admission_digest": receipt["admission_digest"],
            "corpus_pre_admission_digest": "not_provided",
            "safety_case_eligible": False,
            "acceptance_scope": "proposal_only_structural_label_measurement",
        }
    if schema_version not in {
        _LABEL_ADMISSION_RECEIPT_V2,
        _LABEL_ADMISSION_RECEIPT_V3,
        _LABEL_ADMISSION_RECEIPT_V4,
        _LABEL_ADMISSION_RECEIPT_V5,
    }:
        raise SemanticAssuranceEvaluationError("semantic assurance human-label admission receipt version is unsupported")
    is_v6 = schema_version in {_LABEL_ADMISSION_RECEIPT_V3, _LABEL_ADMISSION_RECEIPT_V4, _LABEL_ADMISSION_RECEIPT_V5}
    is_v6_curator_audited = schema_version in {_LABEL_ADMISSION_RECEIPT_V4, _LABEL_ADMISSION_RECEIPT_V5}
    is_v6_strata_bound = schema_version == _LABEL_ADMISSION_RECEIPT_V5
    corpus_pre_admission_digest = _sha256_digest(
        corpus.get("corpus_pre_admission_digest"),
        "semantic assurance corpus pre-admission digest",
    )
    corpus_admission = receipt.get("corpus_admission")
    expected_corpus_admission_keys = (
        _V4_CORPUS_ADMISSION_KEYS
        if is_v6_curator_audited
        else _V3_CORPUS_ADMISSION_KEYS if is_v6 else _V2_CORPUS_ADMISSION_KEYS
    )
    if not isinstance(corpus_admission, Mapping) or set(corpus_admission) != expected_corpus_admission_keys:
        raise SemanticAssuranceEvaluationError("semantic assurance corpus pre-admission binding is invalid")
    _sha256_digest(
        corpus_admission.get("curator_attestation_digest"),
        "semantic assurance v2 curator attestation binding",
    )
    for key in ("dataset_binding_digest", "sealed_case_binding_digest", "sealed_support_binding_digest"):
        _sha256_digest(corpus_admission.get(key), f"semantic assurance v2 {key}")
    if not isinstance(corpus_admission.get("risk_counts"), Mapping) or not isinstance(corpus_admission.get("sealed_risk_counts"), Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance v2 corpus risk binding is invalid")
    rows = [
        {"case_id": str(case["case_id"]), "partition": str(case["partition"]), "risk_class": str(case["risk_class"]), "support_ids": sorted(str(item) for item in case["support_ids"])}
        for case in dataset["cases"]
    ]
    rows.sort(key=lambda row: row["case_id"])
    sealed_rows = [row for row in rows if row["partition"] == "sealed_holdout"]
    risk_counts = {risk: sum(row["risk_class"] == risk for row in rows) for risk in sorted(corpus_admission["risk_counts"])}
    sealed_risk_counts = {risk: sum(row["risk_class"] == risk for row in sealed_rows) for risk in sorted(corpus_admission["sealed_risk_counts"])}
    if corpus_admission.get("dataset_binding_digest") != _digest(rows) or corpus_admission.get("sealed_case_binding_digest") != _digest(sealed_rows) or corpus_admission.get("sealed_support_binding_digest") != _digest([{"case_id": row["case_id"], "support_ids": row["support_ids"]} for row in sealed_rows]) or corpus_admission.get("risk_counts") != risk_counts or corpus_admission.get("sealed_risk_counts") != sealed_risk_counts:
        raise SemanticAssuranceEvaluationError("semantic assurance v2 corpus dataset binding does not match this dataset")
    if _sha256_digest(
        corpus_admission.get("admission_digest"),
        "semantic assurance v2 corpus admission binding",
    ) != corpus_pre_admission_digest:
        raise SemanticAssuranceEvaluationError("semantic assurance v2 corpus pre-admission digest does not bind the receipt")
    result = {
        "admitted": True,
        "status": "attested_structurally_bound_with_corpus_pre_admission_not_identity_authenticated",
        "version": "v2_corpus_pre_admitted",
        "admission_digest": receipt["admission_digest"],
        "corpus_pre_admission_digest": corpus_pre_admission_digest,
        "safety_case_eligible": True,
        "acceptance_scope": "proposal_only_corpus_pre_admitted_human_labelled_measurement",
    }
    if not is_v6:
        return result
    if is_v6_curator_audited:
        curator_audit = _validate_v6_curator_audited_corpus_admission_binding(corpus_admission, rows)
        curator_result = {
            **result,
            "status": "attested_structurally_bound_with_v6_curator_audited_corpus_not_identity_authenticated",
            "version": "v5_v6_curator_audited_strata_bound" if is_v6_strata_bound else "v4_v6_curator_audited",
            "acceptance_scope": "proposal_only_v6_curator_audited_human_labelled_measurement",
            "admission_profile": "v6_curator_audited_independent_human_curation",
            "partition_risk_counts": {
                partition: dict(sorted(corpus_admission["partition_risk_counts"][partition].items()))
                for partition in sorted(PARTITIONS)
            },
            "v6_control_binding_digest": _sha256_digest(
                corpus_admission.get("v6_control_binding_digest"),
                "semantic assurance V6 control binding",
            ),
            "curator_audit": curator_audit,
        }
        if is_v6_strata_bound:
            assurance = receipt.get("assurance")
            if (
                not isinstance(assurance, Mapping)
                or assurance.get("v6_sealed_exact_label_strata_bound") is not True
                or assurance.get("sealed_readiness_strata_minimum_population") != 80
            ):
                raise SemanticAssuranceEvaluationError("semantic assurance V5 strata binding is invalid")
            curator_result["sealed_readiness_strata_binding_digest"] = _sha256_digest(
                assurance.get("sealed_readiness_strata_binding_digest"),
                "semantic assurance V5 strata binding",
            )
            curator_result["sealed_readiness_strata_minimum_population"] = 80
        return curator_result
    _validate_v6_corpus_admission_binding(corpus_admission, rows)
    return {
        **result,
        "status": "attested_structurally_bound_with_v6_corpus_pre_admission_not_identity_authenticated",
        "version": "v3_v6_corpus_pre_admitted",
        "acceptance_scope": "proposal_only_v6_corpus_pre_admitted_human_labelled_measurement",
        "admission_profile": "v6_independent_human_curation",
        "partition_risk_counts": {
            partition: dict(sorted(corpus_admission["partition_risk_counts"][partition].items()))
            for partition in sorted(PARTITIONS)
        },
        "v6_control_binding_digest": _sha256_digest(
            corpus_admission.get("v6_control_binding_digest"),
            "semantic assurance V6 control binding",
        ),
    }


def _validate_v6_corpus_admission_binding(
    corpus_admission: Mapping[str, Any],
    rows: Sequence[Mapping[str, str | list[str]]],
) -> None:
    """Recompute the V6 distribution instead of trusting propagated evidence."""

    if (
        corpus_admission.get("receipt_schema_version") != "bhm.semantic-assurance-corpus-admission-receipt.v2"
        or corpus_admission.get("admission_profile") != "v6_independent_human_curation"
    ):
        raise SemanticAssuranceEvaluationError("semantic assurance V6 corpus admission tier is invalid")
    _sha256_digest(corpus_admission.get("v6_control_binding_digest"), "semantic assurance V6 control binding")
    _validate_v6_partition_risk_distribution(corpus_admission, rows)


def _validate_v6_partition_risk_distribution(
    corpus_admission: Mapping[str, Any],
    rows: Sequence[Mapping[str, str | list[str]]],
) -> None:
    """Recompute the V6 partition/risk grid from this exact dataset."""

    partition_risk_counts = corpus_admission.get("partition_risk_counts")
    if not isinstance(partition_risk_counts, Mapping) or set(partition_risk_counts) != PARTITIONS:
        raise SemanticAssuranceEvaluationError("semantic assurance V6 partition risk distribution is invalid")
    expected = {
        partition: {
            risk: sum(row["partition"] == partition and row["risk_class"] == risk for row in rows)
            for risk in sorted(_V6_RISK_CLASSES)
        }
        for partition in sorted(PARTITIONS)
    }
    for partition in sorted(PARTITIONS):
        observed = partition_risk_counts.get(partition)
        if (
            not isinstance(observed, Mapping)
            or set(observed) != _V6_RISK_CLASSES
            or any(
                not isinstance(observed[risk], int)
                or isinstance(observed[risk], bool)
                or observed[risk] < 1
                for risk in _V6_RISK_CLASSES
            )
            or dict(observed) != expected[partition]
        ):
            raise SemanticAssuranceEvaluationError("semantic assurance V6 partition risk distribution does not bind this dataset")


def _validate_v6_curator_audited_corpus_admission_binding(
    corpus_admission: Mapping[str, Any],
    rows: Sequence[Mapping[str, str | list[str]]],
) -> dict[str, str]:
    """Require the forward-only curator-audited V6 corpus tier.

    This is deliberately separate from :func:`_validate_v6_corpus_admission_binding`.
    A v2 corpus receipt may be structurally V6-complete, but it cannot be
    upgraded to canonical-fragment-audited evidence by adding optional fields.
    """

    if (
        corpus_admission.get("receipt_schema_version")
        != "bhm.semantic-assurance-corpus-admission-receipt.v3"
        or corpus_admission.get("admission_profile")
        != "v6_curator_audited_independent_human_curation"
    ):
        raise SemanticAssuranceEvaluationError("semantic assurance curator-audited corpus admission tier is invalid")
    curator_audit = corpus_admission.get("curator_audit")
    if not isinstance(curator_audit, Mapping) or set(curator_audit) != _V4_CURATOR_AUDIT_KEYS:
        raise SemanticAssuranceEvaluationError("semantic assurance curator audit binding is invalid")
    if curator_audit.get("receipt_schema_version") != "bhm.semantic-assurance-curator-audit-receipt.v1":
        raise SemanticAssuranceEvaluationError("semantic assurance curator audit receipt version is invalid")
    normalized_audit = {
        key: _sha256_digest(curator_audit.get(key), f"semantic assurance curator audit {key}")
        for key in sorted(_V4_CURATOR_AUDIT_KEYS - {"receipt_schema_version"})
    }
    _validate_v6_partition_risk_distribution(corpus_admission, rows)
    return {
        "receipt_schema_version": "bhm.semantic-assurance-curator-audit-receipt.v1",
        **normalized_audit,
    }


def _partition_name(value: Any) -> str:
    return _enum(value, tuple(sorted(PARTITIONS)), "semantic assurance partition")


def _enum(value: Any, allowed: Sequence[str], field: str) -> str:
    text = _text(value, field, 120).casefold()
    if text not in allowed:
        raise SemanticAssuranceEvaluationError(f"{field} is invalid")
    return text


def _text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise SemanticAssuranceEvaluationError(f"{field} is invalid")
    return text


def _sha256_digest(value: Any, field: str) -> str:
    text = _text(value, field, 64).casefold()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SemanticAssuranceEvaluationError(f"{field} must be a SHA-256 digest")
    return text


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


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
