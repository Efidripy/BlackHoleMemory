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
    prediction_digest = _digest(normalized_predictions)
    report_core = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "mode": "offline-content-free-classification-evaluation",
        "dataset": {
            "dataset_id": normalized_dataset["dataset_id"],
            "fixture_digest": normalized_dataset["fixture_digest"],
            "label_digest": normalized_dataset["label_digest"],
            "partition": selected_partition,
            "label_independence": admitted_labels["status"],
            "human_label_admission_digest": admitted_labels["admission_digest"],
        },
        "prediction_digest": prediction_digest,
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
            "acceptance_scope": "proposal_only_human_labelled_measurement" if admitted_labels["admitted"] else "proposal_only_fixture_measurement",
            "safety_case_eligible": mechanical_valid and admitted_labels["admitted"],
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
            "a verified admission structurally binds attested reviewer evidence but cannot authenticate human identity or prove semantic truth",
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
    return {
        "case_id": case["case_id"],
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


def _validate_invocation(raw: Mapping[str, Any] | None) -> dict[str, str]:
    if raw is None:
        return {"model_id": "not_provided", "model_variant": "not_provided", "prompt_digest": "not_provided", "endpoint": "not_provided"}
    if not isinstance(raw, Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance invocation is invalid")
    allowed = {"model_id", "model_variant", "prompt_digest", "endpoint"}
    if set(raw) - allowed:
        raise SemanticAssuranceEvaluationError("semantic assurance invocation contains unsupported fields")
    return {key: _text(raw.get(key) or "not_provided", f"semantic assurance {key}", 256) for key in sorted(allowed)}


def _validate_label_admission(raw: Mapping[str, Any] | None, dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Bind an optional content-free dual-review receipt to this exact corpus.

    The import stays local to avoid a module cycle: the admission contract uses
    the label enums from this module, while this evaluator only needs its final
    receipt verifier.  A missing receipt is explicit lower-tier evidence, not
    a permissive substitute.
    """

    if raw is None:
        return {"admitted": False, "status": "not_verified_by_evaluator", "admission_digest": "not_provided"}
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
    if corpus != expected:
        raise SemanticAssuranceEvaluationError("semantic assurance human-label admission does not bind this exact dataset")
    return {
        "admitted": True,
        "status": "attested_structurally_bound_not_identity_authenticated",
        "admission_digest": receipt["admission_digest"],
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
    "wilson_interval",
    "write_semantic_assurance_receipt",
]
